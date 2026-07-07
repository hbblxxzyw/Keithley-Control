"""
Real instrument driver module.

Implements Keithley 2636/2600B SourceMeter control via PyVISA using TSP commands.
Supports a debug "dry run" mode that requires no physical connection.
Uses the instrument's Trigger Model for high-speed hardware scanning and
chunked data streaming.
"""

import math
import os
import re
import threading
import time
from collections.abc import Callable, Generator

try:
    import pyvisa
except ImportError as exc:
    pyvisa = None
    _PYVISA_IMPORT_ERROR = exc
else:
    _PYVISA_IMPORT_ERROR = None

from core.instrument_base import AbstractSMU
from core.pulse_sequence import PulseEvent, PulseTimelinePoint


class RealKeithley2636(AbstractSMU):
    """
    PyVISA-based driver for the real Keithley 2636 instrument.

    Communicates with the instrument using TSP (Test Script Processor) commands.
    When debug=True, no real commands are sent and no physical connection
    is required, for easier testing and development.
    """

    DEFAULT_TIMEOUT_MS = 10000
    DEFAULT_POLL_INTERVAL_S = 0.02
    MIN_CHUNK_POINTS = 5
    MAX_POINTS_PER_PRINTBUFFER = 5
    MEASURE_EVERY_N_POINTS = 8
    DEFAULT_RESOURCE_ENCODING = "latin-1"
    DUAL_SYNC_SCRIPT_NAME = "oai_dualsync"
    PULSE_SCRIPT_NAME = "oai_pulse"
    TSP_TABLE_CHUNK_SIZE = 14
    TSP_FINAL_ENDPULSE_DELAY_S = 5e-7

    def __init__(self, debug: bool = False) -> None:
        self.debug = debug
        self._rm = None if debug else self._create_resource_manager()
        self._resource = None
        self._resource_address: str | None = None
        self._current_limits: dict[str, float] = {}
        self._source_levels: dict[str, float] = {"smua": 0.0, "smub": 0.0}
        self._source_limits: dict[str, float | None] = {"smua": None, "smub": None}
        self._source_modes: dict[str, str] = {}
        self._measure_state: dict[str, dict[str, object]] = {}
        self._instrument_model = "2636B" if debug else None
        self._dual_sync_script_loaded = bool(debug)
        self._pulse_script_loaded = bool(debug)
        self._io_lock = threading.RLock()

    @staticmethod
    def _create_resource_manager():
        if pyvisa is None:
            raise RuntimeError(
                "PyVISA is not installed. Install pyvisa, or run with the dummy "
                "instrument for GUI development."
            ) from _PYVISA_IMPORT_ERROR

        requested_backend = os.environ.get("KEITHLEY_VISA_BACKEND", "").strip()
        if requested_backend:
            return pyvisa.ResourceManager(requested_backend)

        errors: list[str] = []
        for backend in (None, "@py"):
            try:
                if backend is None:
                    return pyvisa.ResourceManager()
                return pyvisa.ResourceManager(backend)
            except Exception as exc:
                label = "default" if backend is None else backend
                errors.append(f"{label}: {exc}")

        details = "; ".join(errors)
        raise RuntimeError(
            "Could not load a VISA backend. Install NI-VISA, install pyvisa-py, "
            "or run with the dummy instrument for GUI development. "
            f"Tried {details}"
        )

    def _recreate_resource_manager(self) -> None:
        close = getattr(self._rm, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
        self._rm = None
        self._rm = self._create_resource_manager()

    def _close_resource_quietly(self) -> None:
        resource = self._resource
        self._resource = None
        self._resource_address = None
        if resource is None:
            return
        try:
            resource.close()
        except Exception:
            pass

    @classmethod
    def visa_available(cls) -> tuple[bool, str | None]:
        """Return whether PyVISA can load a usable VISA backend on this machine."""
        try:
            rm = cls._create_resource_manager()
        except Exception as exc:
            return False, str(exc)

        close = getattr(rm, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
        return True, None

    def _send_cmd(self, cmd: str) -> None:
        if self.debug:
            print(f"[DEBUG SEND] {cmd}")
            return
        if self._resource is None:
            raise RuntimeError("Instrument not connected; call connect() first.")
        with self._io_lock:
            self._resource.write(cmd)

    def _query_cmd(self, cmd: str) -> str:
        if self.debug:
            return "1.23e-6"
        if self._resource is None:
            raise RuntimeError("Instrument not connected; call connect() first.")
        with self._io_lock:
            self._resource.write(cmd)
            return self._read_response().strip()

    def _read_response(self) -> str:
        if self._resource is None:
            raise RuntimeError("Instrument not connected; call connect() first.")

        raw = self._resource.read_raw()
        if not raw:
            return ""

        for encoding, errors in (
            ("utf-8", "strict"),
            (self.DEFAULT_RESOURCE_ENCODING, "strict"),
            ("ascii", "ignore"),
        ):
            try:
                return raw.decode(encoding, errors=errors).strip("\x00\r\n ")
            except UnicodeDecodeError:
                continue

        return raw.decode(self.DEFAULT_RESOURCE_ENCODING, errors="replace").strip(
            "\x00\r\n "
        )

    def _read_response_with_timeout(self, timeout_ms: int) -> str:
        if self._resource is None:
            raise RuntimeError("Instrument not connected; call connect() first.")

        original_timeout = getattr(self._resource, "timeout", None)
        try:
            self._resource.timeout = timeout_ms
            return self._read_response()
        finally:
            if original_timeout is not None:
                self._resource.timeout = original_timeout

    def _drain_output_queue(self, max_reads: int = 8) -> list[str]:
        if self.debug or self._resource is None:
            return []

        drained: list[str] = []
        with self._io_lock:
            for _ in range(max_reads):
                try:
                    reply = self._read_response_with_timeout(100)
                except Exception:
                    break
                if not reply:
                    break
                drained.append(reply)
        return drained

    def _write_tsp_script_block(self, script_name: str, script_body: str) -> None:
        if self._resource is None:
            raise RuntimeError("Instrument not connected; call connect() first.")

        self._resource.write(f"loadandrunscript {script_name}")
        for line in script_body.strip("\r\n").splitlines():
            self._resource.write(line.rstrip() if line.strip() else "--")
        self._resource.write("endscript")

    def _load_tsp_script(self, script_name: str, script_body: str) -> None:
        # FIX: Use `loadandrunscript` so the named script is compiled and executed immediately.
        if self.debug:
            print(f"[DEBUG LOADSCRIPT] {script_name}")
            print(script_body.strip())
            return
        if self._resource is None:
            raise RuntimeError("Instrument not connected; call connect() first.")
        # FIX: Clear stale parser/runtime errors before attempting to load a script.
        with self._io_lock:
            self._drain_output_queue()
            self._send_cmd("*CLS")
            self._send_cmd("errorqueue.clear()")
            self._write_tsp_script_block(script_name, script_body)
            # FIX: Fail fast if the script produced any syntax/runtime error while loading or running.
            errors = self.dump_errors()
            if errors:
                # FIX: Dump numbered script lines so Keithley TSP line numbers can be matched quickly.
                self._debug_dump_script_lines(script_body)
                error_lines = "\n".join(
                    f"  {index}. {entry}" for index, entry in enumerate(errors, start=1)
                )
                raise RuntimeError(
                    f"Failed to load TSP script '{script_name}'.\nError queue:\n{error_lines}"
                )
            # FIX: Verify the script run registered callable global functions.
            type_reply = self._query_cmd_checked(
                "print(type(configure_block), type(wait_done))",
                f"verifying TSP globals for '{script_name}'",
            )
        type_tokens = [
            token.strip().lower()
            for token in re.split(r"[\s,]+", type_reply)
            if token
        ]
        if len(type_tokens) < 2 or type_tokens[0] != "function" or type_tokens[1] != "function":
            raise RuntimeError(
                "Loaded TSP functions are not callable globals. "
                f"configure_block={type_tokens[0] if type_tokens else '<missing>'}, "
                f"wait_done={type_tokens[1] if len(type_tokens) > 1 else '<missing>'}"
            )

    def _debug_dump_script_lines(self, script_body: str) -> None:
        # FIX: Print numbered TSP script lines to help map Keithley error line numbers.
        for line_no, line_text in enumerate(script_body.strip("\r\n").splitlines(), start=1):
            print(f"[TSP {line_no:03d}] {line_text}")

    def _load_tsp_script_with_functions(
        self,
        script_name: str,
        script_body: str,
        function_names: list[str],
    ) -> None:
        if self.debug:
            print(f"[DEBUG LOADSCRIPT] {script_name}")
            print(script_body.strip())
            return
        if self._resource is None:
            raise RuntimeError("Instrument not connected; call connect() first.")

        with self._io_lock:
            self._drain_output_queue()
            self._send_cmd("*CLS")
            self._send_cmd("errorqueue.clear()")
            self._write_tsp_script_block(script_name, script_body)
            errors = self.dump_errors()
            if errors:
                self._debug_dump_script_lines(script_body)
                error_lines = "\n".join(
                    f"  {index}. {entry}" for index, entry in enumerate(errors, start=1)
                )
                raise RuntimeError(
                    f"Failed to load TSP script '{script_name}'.\nError queue:\n{error_lines}"
                )

            expr = ", ".join(f"type({name})" for name in function_names)
            type_reply = self._query_cmd_checked(
                f"print({expr})",
                f"verifying TSP globals for '{script_name}'",
            )
        type_tokens = [
            token.strip().lower()
            for token in re.split(r"[\s,]+", type_reply)
            if token
        ]
        if len(type_tokens) < len(function_names) or any(
            token != "function" for token in type_tokens[: len(function_names)]
        ):
            details = ", ".join(
                f"{name}={type_tokens[index] if index < len(type_tokens) else '<missing>'}"
                for index, name in enumerate(function_names)
            )
            raise RuntimeError(f"Loaded TSP pulse functions are not callable: {details}")

    def _error_queue_count(self) -> int:
        if self.debug:
            return 0
        with self._io_lock:
            replies = [self._query_cmd("print(errorqueue.count)")]
            for _ in range(2):
                try:
                    return int(float(replies[-1]))
                except ValueError:
                    try:
                        replies.append(self._read_response_with_timeout(250).strip())
                    except Exception:
                        break

        details = ", ".join(repr(reply) for reply in replies if reply)
        raise RuntimeError(
            "Expected numeric reply from errorqueue.count, got "
            f"{details or '<empty>'}. Instrument responses may be out of sync."
        )

    def _raise_if_error_queue(self, context: str) -> None:
        count = self._error_queue_count()
        if count <= 0:
            return
        errors = self.dump_errors()
        message = [f"Instrument error after {context}."]
        if errors:
            message.append("Error queue:")
            message.extend(f"  {index}. {entry}" for index, entry in enumerate(errors, start=1))
        raise RuntimeError("\n".join(message))

    def _send_cmd_checked(self, cmd: str, context: str) -> None:
        with self._io_lock:
            self._send_cmd(cmd)
            self._raise_if_error_queue(context)

    def _query_cmd_checked(self, cmd: str, context: str) -> str:
        with self._io_lock:
            reply = self._query_cmd(cmd)
            self._raise_if_error_queue(context)
            return reply

    def _ensure_ascii_stream_format(self) -> None:
        if self.debug:
            return
        # TODO: Evaluate format.data = format.REAL32 to reduce sweep streaming payload.
        self._send_cmd("format.data = format.ASCII")
        self._send_cmd("format.asciiprecision = 12")
        self._send_cmd("localnode.prompts = 0")
        self._send_cmd("localnode.prompts4882 = 0")

    def _clear_error_state(self) -> None:
        if self.debug:
            return
        # FIX: Clear stale SCPI/TSP state before a sweep starts.
        self._send_cmd("*CLS")
        self._send_cmd("errorqueue.clear()")

    def _recover_from_sweep_error(self) -> None:
        if self.debug:
            return
        for cmd in ("smua.abort()", "smub.abort()", "*CLS", "errorqueue.clear()"):
            try:
                # FIX: Abort both channels and clear queued errors after any sweep failure.
                self._send_cmd(cmd)
            except Exception:
                pass

    def dump_errors(self) -> list[str]:
        if self.debug:
            return []
        # FIX: Expose instrument error queue contents for field debugging.
        count = self._error_queue_count()
        errors: list[str] = []
        for _ in range(max(count, 0)):
            errors.append(self._query_cmd("print(errorqueue.next())"))
        return errors

    def abort_sweep(self) -> None:
        if self.debug:
            print("[DEBUG] Abort sweep requested")
            return
        for cmd in ("smua.abort()", "smub.abort()"):
            try:
                self._send_cmd(cmd)
            except Exception:
                pass

    def _build_dual_sync_script(self) -> str:
        # FIX: Define helper and entrypoint functions as globals to avoid named-script namespace conflicts.
        return """
function _pb(x)
    x.nvbuffer1.clear()
    x.nvbuffer1.clearcache()
    x.nvbuffer1.appendmode = 1
    x.nvbuffer1.collectsourcevalues = 1
    x.nvbuffer1.collecttimestamps = 1

    x.nvbuffer2.clear()
    x.nvbuffer2.clearcache()
    x.nvbuffer2.appendmode = 1
    x.nvbuffer2.collectsourcevalues = 1
    x.nvbuffer2.collecttimestamps = 1
end

function _lin(a, b, n)
    local vals = {}
    local dv = 0
    if n > 1 then
        dv = (b - a) / (n - 1)
    end
    for i = 1, n do
        vals[i] = a + (i - 1) * dv
    end
    return vals
end

function configure_block(pn, sn, p0, p1, n, dt, plim, slim, smode, slev, s0, s1, cpv, csv)
    local p = _G[pn]
    local s = _G[sn]
    if p == nil then
        error("Unknown primary SMU: " .. tostring(pn))
    end
    if s == nil then
        error("Unknown secondary SMU: " .. tostring(sn))
    end

    if dt == nil or dt < 0 then
        dt = 0
    end

    local svals = nil
    if smode == 1 then
        svals = _lin(s0, s1, n)
    end

    p.abort()
    s.abort()
    trigger.blender[1].reset()
    trigger.blender[2].reset()
    trigger.timer[1].reset()

    _pb(p)
    _pb(s)

    p.source.func = p.OUTPUT_DCVOLTS
    s.source.func = s.OUTPUT_DCVOLTS

    if plim ~= nil and plim > 0 then
        p.source.limiti = plim
    end
    if slim ~= nil and slim > 0 then
        s.source.limiti = slim
    end

    p.source.levelv = p0
    if smode == 1 then
        s.source.levelv = s0
    else
        s.source.levelv = slev
    end
    p.source.delay = 0
    s.source.delay = 0
    p.measure.delay = 0
    s.measure.delay = 0

    p.trigger.source.linearv(p0, p1, n)
    p.trigger.source.action = p.ENABLE
    if smode == 1 then
        s.trigger.source.listv(svals)
        s.trigger.source.action = s.ENABLE
    else
        s.trigger.source.action = s.DISABLE
    end

    if cpv ~= 0 then
        p.trigger.measure.iv(p.nvbuffer1, p.nvbuffer2)
    else
        p.trigger.measure.i(p.nvbuffer1)
    end
    if csv ~= 0 then
        s.trigger.measure.iv(s.nvbuffer1, s.nvbuffer2)
    else
        s.trigger.measure.i(s.nvbuffer1)
    end

    p.trigger.measure.action = p.ENABLE
    s.trigger.measure.action = s.ENABLE
    p.trigger.count = n
    s.trigger.count = n
    p.trigger.arm.count = 1
    s.trigger.arm.count = 1

    p.trigger.arm.stimulus = 0
    s.trigger.arm.stimulus = 0

    trigger.timer[1].delay = dt
    trigger.timer[1].count = 1
    trigger.timer[1].passthrough = false
    trigger.timer[1].stimulus = p.trigger.SOURCE_COMPLETE_EVENT_ID

    p.trigger.measure.stimulus = trigger.timer[1].EVENT_ID
    s.trigger.measure.stimulus = trigger.timer[1].EVENT_ID

    trigger.blender[1].orenable = true
    trigger.blender[1].stimulus[1] = p.trigger.ARMED_EVENT_ID
    trigger.blender[1].stimulus[2] = p.trigger.PULSE_COMPLETE_EVENT_ID
    p.trigger.source.stimulus = trigger.blender[1].EVENT_ID
    if smode == 1 then
        s.trigger.source.stimulus = trigger.blender[1].EVENT_ID
    else
        s.trigger.source.stimulus = 0
    end

    trigger.blender[2].orenable = false
    trigger.blender[2].stimulus[1] = p.trigger.MEASURE_COMPLETE_EVENT_ID
    trigger.blender[2].stimulus[2] = s.trigger.MEASURE_COMPLETE_EVENT_ID
    p.trigger.endpulse.stimulus = trigger.blender[2].EVENT_ID
    if smode == 1 then
        s.trigger.endpulse.stimulus = trigger.blender[2].EVENT_ID
    else
        s.trigger.endpulse.stimulus = 0
    end

    p.trigger.endpulse.action = p.SOURCE_HOLD
    s.trigger.endpulse.action = s.SOURCE_HOLD
    p.trigger.endsweep.action = p.SOURCE_HOLD
    s.trigger.endsweep.action = s.SOURCE_HOLD

    p.source.output = p.OUTPUT_ON
    s.source.output = s.OUTPUT_ON

    s.trigger.initiate()
    p.trigger.initiate()
end

function configure_single_block(cn, on, c0, c1, n, dt, clim, cpv)
    local c = _G[cn]
    local o = _G[on]
    if c == nil then
        error("Unknown active SMU: " .. tostring(cn))
    end

    if dt == nil or dt < 0 then
        dt = 0
    end

    c.abort()
    if o ~= nil then
        o.abort()
        o.source.output = o.OUTPUT_OFF
        o.trigger.source.action = o.DISABLE
        o.trigger.measure.action = o.DISABLE
    end
    trigger.blender[1].reset()
    trigger.blender[2].reset()
    trigger.timer[1].reset()

    _pb(c)

    c.source.func = c.OUTPUT_DCVOLTS
    if clim ~= nil and clim > 0 then
        c.source.limiti = clim
    end

    c.source.levelv = c0
    c.source.delay = 0
    c.measure.delay = 0

    c.trigger.source.linearv(c0, c1, n)
    c.trigger.source.action = c.ENABLE

    if cpv ~= 0 then
        c.trigger.measure.iv(c.nvbuffer1, c.nvbuffer2)
    else
        c.trigger.measure.i(c.nvbuffer1)
    end

    c.trigger.measure.action = c.ENABLE
    c.trigger.count = n
    c.trigger.arm.count = 1
    c.trigger.arm.stimulus = 0

    trigger.timer[1].delay = dt
    trigger.timer[1].count = 1
    trigger.timer[1].passthrough = false
    trigger.timer[1].stimulus = c.trigger.SOURCE_COMPLETE_EVENT_ID

    c.trigger.measure.stimulus = trigger.timer[1].EVENT_ID

    trigger.blender[1].orenable = true
    trigger.blender[1].stimulus[1] = c.trigger.ARMED_EVENT_ID
    trigger.blender[1].stimulus[2] = c.trigger.PULSE_COMPLETE_EVENT_ID
    c.trigger.source.stimulus = trigger.blender[1].EVENT_ID

    c.trigger.endpulse.stimulus = c.trigger.MEASURE_COMPLETE_EVENT_ID
    c.trigger.endpulse.action = c.SOURCE_HOLD
    c.trigger.endsweep.action = c.SOURCE_HOLD

    c.source.output = c.OUTPUT_ON
    c.trigger.initiate()
end

function wait_done()
    waitcomplete()
end
        """

    def _ensure_dual_sync_script_loaded(self) -> None:
        if self._dual_sync_script_loaded:
            return
        if self.debug:
            self._dual_sync_script_loaded = True
            return

        # FIX: Load the script as one named TSP script block.
        self._load_tsp_script(
            self.DUAL_SYNC_SCRIPT_NAME,
            self._build_dual_sync_script(),
        )
        self._dual_sync_script_loaded = True

    def _parse_numeric_tokens(self, reply: str) -> list[float]:
        values: list[float] = []
        for token in re.split(r"[\s,]+", reply.strip()):
            if not token:
                continue
            try:
                values.append(float(token))
            except ValueError:
                continue
        return values

    def _reshape_printbuffer_rows(
        self, reply: str, column_count: int
    ) -> list[list[float]]:
        if column_count <= 0:
            return []
        flat_values = self._parse_numeric_tokens(reply)
        usable = len(flat_values) - (len(flat_values) % column_count)
        rows: list[list[float]] = []
        for index in range(0, usable, column_count):
            rows.append(flat_values[index : index + column_count])
        return rows

    def _format_tsp_value(self, value: float | int | str | None) -> str:
        if value is None:
            return "nil"
        if isinstance(value, bool):
            return "1" if value else "0"
        if isinstance(value, str):
            # FIX: Quote string arguments so TSP receives a literal name instead of a bare global.
            return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'
        if isinstance(value, float):
            return repr(float(value))
        return str(value)

    def _format_tsp_table(self, values: list[float]) -> str:
        return "{" + ", ".join(self._format_tsp_value(float(value)) for value in values) + "}"

    def _load_tsp_numeric_table(self, name: str, values: list[float]) -> None:
        if self.debug:
            return
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise ValueError(f"Invalid TSP table name: {name!r}")

        with self._io_lock:
            self._send_cmd(f"{name} = {{}}")
            for start in range(0, len(values), self.TSP_TABLE_CHUNK_SIZE):
                chunk = values[start : start + self.TSP_TABLE_CHUNK_SIZE]
                insert_commands = [
                    f"table.insert({name}, {self._format_tsp_value(float(value))})"
                    for value in chunk
                ]
                self._send_cmd(" ".join(insert_commands))
            self._raise_if_error_queue(f"loading TSP table {name}")

    def connect(self, resource_str: str) -> bool:
        if self.debug:
            print(f"[DEBUG] Virtual connection OK: {resource_str}")
            return True
        if self._rm is None:
            try:
                self._rm = self._create_resource_manager()
            except Exception as exc:
                print(f"Connection error: {exc}")
                return False
        self._close_resource_quietly()

        last_error: Exception | None = None
        for attempt in range(2):
            try:
                if attempt > 0:
                    self._recreate_resource_manager()
                self._resource = self._rm.open_resource(resource_str)
                break
            except Exception as exc:
                last_error = exc
                self._close_resource_quietly()
        else:
            print(f"Connection error: {last_error}")
            return False

        try:
            self._resource.timeout = self.DEFAULT_TIMEOUT_MS
            self._resource.encoding = self.DEFAULT_RESOURCE_ENCODING
            self._resource.read_termination = "\n"
            self._resource.write_termination = "\n"
            self._resource_address = resource_str

            if "ASRL" in resource_str.upper() or "COM" in resource_str.upper():
                self._resource.baud_rate = 57600

            self._ensure_ascii_stream_format()
            self._dual_sync_script_loaded = False
            self._pulse_script_loaded = False
            return True
        except Exception as exc:
            self._close_resource_quietly()
            print(f"Connection error: {exc}")
            return False

    def disconnect(self) -> None:
        if self.debug:
            print("[DEBUG] Virtual disconnect")
            return
        self._close_resource_quietly()
        self._instrument_model = None
        self._dual_sync_script_loaded = False
        self._pulse_script_loaded = False

    def is_connected(self, timeout_ms: int = 300) -> bool:
        if self.debug:
            return True
        if self._resource is None:
            return False

        if self._resource_address:
            resources: list[str] | None
            try:
                resources = list(self._rm.list_resources()) if self._rm is not None else []
            except Exception:
                resources = None
            if resources is not None and self._resource_address not in resources:
                return False

        with self._io_lock:
            original_timeout = getattr(self._resource, "timeout", None)
            try:
                self._resource.timeout = max(50, int(timeout_ms))
                self._resource.write("print(1)")
                reply = self._read_response().strip()
                return abs(float(reply) - 1.0) < 1e-12
            except Exception:
                return False
            finally:
                if original_timeout is not None and self._resource is not None:
                    try:
                        self._resource.timeout = original_timeout
                    except Exception:
                        pass

    def get_model(self) -> str | None:
        if self.debug:
            return self._instrument_model or "2636B"
        if self._resource is None:
            return self._instrument_model
        if self._instrument_model:
            return self._instrument_model

        for query in ("print(localnode.model)", "*IDN?"):
            try:
                reply = self._query_cmd(query)
            except Exception:
                continue
            match = re.search(r"\b(260[124]B|261[124]B|263[456]B)\b", reply.upper())
            if match:
                self._instrument_model = match.group(1)
                return self._instrument_model

        return None

    def find_resource_address(self, preferred_serial: str | None = None) -> str | None:
        if self._rm is None:
            try:
                self._rm = self._create_resource_manager()
            except Exception:
                return None
        try:
            resources = list(self._rm.list_resources())
        except Exception:
            try:
                self._recreate_resource_manager()
                resources = list(self._rm.list_resources())
            except Exception:
                return None

        if preferred_serial:
            for resource in resources:
                if preferred_serial in resource:
                    return resource

        for resource in resources:
            if "USB" in resource.upper() and "::1510::" in resource:
                return resource

        for resource in resources:
            if resource.upper().startswith(("USB", "TCPIP", "ASRL")):
                return resource

        return None

    def set_output(self, smu_channel: str, state: bool) -> None:
        on_off = "OUTPUT_ON" if state else "OUTPUT_OFF"
        self._send_cmd_checked(
            f"{smu_channel}.source.output = {smu_channel}.{on_off}",
            f"setting {smu_channel} output {str(state).lower()}",
        )

    def set_voltage_source(
        self, smu_channel: str, voltage: float, current_limit: float
    ) -> None:
        if self._source_modes.get(smu_channel) != "voltage":
            self._send_cmd_checked(
                f"{smu_channel}.source.func = {smu_channel}.OUTPUT_DCVOLTS",
                f"configuring {smu_channel} voltage source mode",
            )
            self._source_modes[smu_channel] = "voltage"

        last_limit = self._source_limits.get(smu_channel)
        if last_limit is None or abs(last_limit - float(current_limit)) > 1e-15:
            self._send_cmd_checked(
                f"{smu_channel}.source.limiti = {current_limit}",
                f"setting {smu_channel} current limit",
            )
            self._source_limits[smu_channel] = float(current_limit)

        last_level = self._source_levels.get(smu_channel)
        if last_level is None or abs(last_level - float(voltage)) > 1e-15:
            self._send_cmd_checked(
                f"{smu_channel}.source.levelv = {voltage}",
                f"setting {smu_channel} source voltage",
            )
        self._source_levels[smu_channel] = float(voltage)
        if current_limit > 0:
            self._current_limits[smu_channel] = float(current_limit)

    def set_current_source(
        self, smu_channel: str, current: float, voltage_limit: float
    ) -> None:
        if self._source_modes.get(smu_channel) != "current":
            self._send_cmd_checked(
                f"{smu_channel}.source.func = {smu_channel}.OUTPUT_DCAMPS",
                f"configuring {smu_channel} current source mode",
            )
            self._source_modes[smu_channel] = "current"

        last_limit = self._source_limits.get(smu_channel)
        if last_limit is None or abs(last_limit - float(voltage_limit)) > 1e-15:
            self._send_cmd_checked(
                f"{smu_channel}.source.limitv = {voltage_limit}",
                f"setting {smu_channel} voltage limit",
            )
            self._source_limits[smu_channel] = float(voltage_limit)

        last_level = self._source_levels.get(smu_channel)
        if last_level is None or abs(last_level - float(current)) > 1e-15:
            self._send_cmd_checked(
                f"{smu_channel}.source.leveli = {current}",
                f"setting {smu_channel} source current",
            )
        self._source_levels[smu_channel] = float(current)

    def measure_current(self, smu_channel: str) -> float:
        reply = self._query_cmd_checked(
            f"print({smu_channel}.measure.i())",
            f"measuring current on {smu_channel}",
        )
        return float(reply)

    def _measurement_range_amps(self, current_range: str) -> float | None:
        range_map = {
            "100 pA": 100e-12,
            "1 nA": 1e-9,
            "10 nA": 10e-9,
            "100 nA": 100e-9,
            "1 uA": 1e-6,
            "10 uA": 10e-6,
            "100 uA": 100e-6,
            "1 mA": 1e-3,
            "10 mA": 10e-3,
            "100 mA": 100e-3,
            "1 A": 1.0,
        }
        return range_map.get(str(current_range or "").strip())

    def _apply_current_measure_range(self, smu_channel: str) -> None:
        state = self._measure_state.get(smu_channel, {})
        range_amps = state.get("range_amps")
        if isinstance(range_amps, (int, float)) and range_amps > 0:
            self._send_cmd_checked(
                f"{smu_channel}.measure.autorangei = {smu_channel}.AUTORANGE_OFF",
                f"disabling {smu_channel} current autorange",
            )
            self._send_cmd_checked(
                f"{smu_channel}.measure.rangei = {float(range_amps)}",
                f"setting {smu_channel} current range",
            )
        else:
            self._send_cmd_checked(
                f"{smu_channel}.measure.autorangei = {smu_channel}.AUTORANGE_ON",
                f"enabling {smu_channel} current autorange",
            )

    def configure_measurement(
        self,
        smu_channel: str,
        measurement_items: list[str],
        current_range: str,
        autozero: str,
        nplc: float,
    ) -> None:
        range_amps = self._measurement_range_amps(current_range)
        self._measure_state[smu_channel] = {
            "count": 0,
            "signature": tuple(measurement_items),
            "last": {},
            "current_range": str(current_range or "").strip() or "Auto",
            "range_amps": range_amps,
            "nplc": float(nplc),
        }

        autozero_map = {
            "off": "AUTOZERO_OFF",
            "once": "AUTOZERO_ONCE",
            "on": "AUTOZERO_AUTO",
            "auto": "AUTOZERO_AUTO",
        }
        autozero_token = autozero_map.get(
            str(autozero or "").strip().lower(),
            "AUTOZERO_AUTO",
        )

        self._send_cmd_checked(
            f"{smu_channel}.measure.nplc = {float(nplc)}",
            f"setting {smu_channel} NPLC",
        )
        self._send_cmd_checked(
            f"{smu_channel}.measure.autozero = {smu_channel}.{autozero_token}",
            f"setting {smu_channel} autozero",
        )
        self._send_cmd_checked(
            f"{smu_channel}.measure.filter.enable = {smu_channel}.FILTER_OFF",
            f"disabling {smu_channel} measurement filter",
        )

        self._apply_current_measure_range(smu_channel)

    def measure_selected(
        self, smu_channel: str, measurement_items: list[str]
    ) -> dict[str, float]:
        command_map = {
            "Voltage": "v",
            "Current": "i",
            "Resistance": "r",
        }
        if self.debug:
            voltage = float(self._source_levels.get(smu_channel, 0.0))
            current = 1.23e-6 + voltage * 1e-7
            resistance = voltage / current if abs(current) > 1e-15 else float("inf")
            fake_values = {
                "Voltage": voltage,
                "Current": current,
                "Resistance": resistance,
            }
            return {
                item: fake_values[item]
                for item in measurement_items
                if item in fake_values
            }

        if not measurement_items:
            return {}

        state = self._measure_state.setdefault(
            smu_channel,
            {"count": 0, "signature": tuple(measurement_items), "last": {}},
        )
        signature = tuple(measurement_items)
        if state.get("signature") != signature:
            state["count"] = 0
            state["signature"] = signature
            state["last"] = {}

        state["count"] = int(state.get("count", 0)) + 1
        last_values = state.get("last", {})
        if (
            self.MEASURE_EVERY_N_POINTS > 1
            and last_values
            and (int(state["count"]) - 1) % self.MEASURE_EVERY_N_POINTS != 0
        ):
            return dict(last_values)

        selected = [
            (item, command_map[item])
            for item in measurement_items
            if item in command_map
        ]
        if not selected:
            return {}

        expr = ", ".join(f"{smu_channel}.measure.{suffix}()" for _, suffix in selected)
        reply = self._query_cmd_checked(
            f"print({expr})",
            f"reading selected measurements on {smu_channel}",
        )
        parts = [part for part in re.split(r"[\t,\r\n ]+", reply.strip()) if part]

        out: dict[str, float] = {}
        for (item, _), value in zip(selected, parts):
            out[item] = float(value)
        state["last"] = dict(out)
        return out

    def _build_pulse_script(self) -> str:
        return """
function _pulse_pb(c)
    c.nvbuffer1.clear()
    c.nvbuffer1.clearcache()
    c.nvbuffer1.appendmode = 1
    c.nvbuffer1.collectsourcevalues = 1
    c.nvbuffer1.collecttimestamps = 1
    c.nvbuffer2.clear()
    c.nvbuffer2.clearcache()
    c.nvbuffer2.appendmode = 1
    c.nvbuffer2.collectsourcevalues = 1
    c.nvbuffer2.collecttimestamps = 1
end

function configure_pulse(cn, on, smode, levels, widths, periods, n, lim)
    local c = _G[cn]
    local o = _G[on]
    if c == nil then
        error("Unknown pulse SMU: " .. tostring(cn))
    end

    c.abort()
    if o ~= nil then
        o.abort()
        o.source.output = o.OUTPUT_OFF
        o.trigger.source.action = o.DISABLE
        o.trigger.measure.action = o.DISABLE
    end
    trigger.timer[2].reset()
    trigger.timer[3].reset()

    _pulse_pb(c)

    if smode == 1 then
        c.source.func = c.OUTPUT_DCAMPS
        if lim ~= nil and lim > 0 then
            c.source.limitv = lim
        end
        c.source.leveli = 0
        c.trigger.source.listi(levels)
        c.trigger.measure.v(c.nvbuffer1)
    else
        c.source.func = c.OUTPUT_DCVOLTS
        if lim ~= nil and lim > 0 then
            c.source.limiti = lim
        end
        c.source.levelv = 0
        c.trigger.source.listv(levels)
        c.trigger.measure.i(c.nvbuffer1)
    end

    c.source.delay = 0
    c.measure.delay = 0
    c.trigger.source.action = c.ENABLE
    c.trigger.measure.action = c.ENABLE
    c.trigger.count = n
    c.trigger.arm.count = 1
    c.trigger.arm.stimulus = 0

    c.trigger.measure.stimulus = c.trigger.SOURCE_COMPLETE_EVENT_ID

    trigger.timer[3].delaylist = widths
    trigger.timer[3].passthrough = false
    trigger.timer[3].count = 1

    if n > 1 then
        trigger.timer[2].delaylist = periods
        trigger.timer[2].count = n - 1
        trigger.timer[2].passthrough = true
        trigger.timer[2].stimulus = c.trigger.SWEEPING_EVENT_ID
        c.trigger.source.stimulus = trigger.timer[2].EVENT_ID
        trigger.timer[3].stimulus = trigger.timer[2].EVENT_ID
    else
        c.trigger.source.stimulus = 0
        trigger.timer[3].stimulus = c.trigger.ARMED_EVENT_ID
    end

    c.trigger.endpulse.action = c.SOURCE_IDLE
    c.trigger.endpulse.stimulus = trigger.timer[3].EVENT_ID
    c.trigger.endsweep.action = c.SOURCE_IDLE

    c.source.output = c.OUTPUT_ON
    c.trigger.initiate()
end

function configure_pulse_list_sweep(cn, on, smode, levels, n, dt, lim, bmode, blev, blim, measure_bias)
    local c = _G[cn]
    local b = nil
    if on ~= nil then
        b = _G[on]
    end
    if c == nil then
        error("Unknown pulse SMU: " .. tostring(cn))
    end
    if dt == nil or dt < 0 then
        dt = 0
    end

    c.abort()
    if b ~= nil then
        b.abort()
    end
    trigger.blender[1].reset()
    trigger.blender[2].reset()
    trigger.blender[3].reset()
    trigger.timer[1].reset()

    _pulse_pb(c)
    if b ~= nil then
        _pulse_pb(b)
    end

    if smode == 1 then
        c.source.func = c.OUTPUT_DCAMPS
        c.source.offmode = c.OUTPUT_NORMAL
        if lim ~= nil and lim > 0 then
            c.source.limitv = lim
        end
        c.source.leveli = 0
        c.trigger.source.listi(levels)
    else
        c.source.func = c.OUTPUT_DCVOLTS
        c.source.autorangev = c.AUTORANGE_ON
        c.trigger.source.limiti = 0
        c.source.highc = c.DISABLE
        c.source.offmode = c.OUTPUT_NORMAL
        if lim ~= nil and lim > 0 then
            c.source.limiti = lim
        end
        c.source.levelv = 0
        c.trigger.source.listv(levels)
    end

    c.source.delay = 0
    c.measure.delay = 0
    c.trigger.source.action = c.ENABLE
    c.trigger.measure.iv(c.nvbuffer1, c.nvbuffer2)
    c.trigger.measure.action = c.ENABLE
    c.trigger.count = n
    c.trigger.arm.count = 1
    c.trigger.arm.stimulus = 0

    trigger.timer[1].delay = dt
    trigger.timer[1].count = 1
    trigger.timer[1].passthrough = false
    trigger.timer[1].stimulus = trigger.blender[1].EVENT_ID

    trigger.blender[1].orenable = true
    trigger.blender[1].stimulus[1] = c.trigger.ARMED_EVENT_ID
    trigger.blender[1].stimulus[2] = trigger.blender[3].EVENT_ID
    c.trigger.source.stimulus = trigger.blender[1].EVENT_ID

    trigger.blender[2].orenable = false
    trigger.blender[2].stimulus[1] = c.trigger.SOURCE_COMPLETE_EVENT_ID
    trigger.blender[2].stimulus[3] = trigger.timer[1].EVENT_ID
    c.trigger.measure.stimulus = trigger.blender[2].EVENT_ID

    trigger.blender[3].orenable = false
    trigger.blender[3].stimulus[1] = c.trigger.PULSE_COMPLETE_EVENT_ID

    c.trigger.endpulse.action = c.SOURCE_HOLD
    c.trigger.endpulse.stimulus = c.trigger.MEASURE_COMPLETE_EVENT_ID
    c.trigger.endsweep.action = c.SOURCE_HOLD

    if b ~= nil then
        if bmode == 1 then
            b.source.func = b.OUTPUT_DCAMPS
            b.source.offmode = b.OUTPUT_NORMAL
            if blim ~= nil and blim > 0 then
                b.source.limitv = blim
            end
            b.source.leveli = blev
            b.trigger.source.lineari(blev, blev, 10)
        else
            b.source.func = b.OUTPUT_DCVOLTS
            if math.abs(blev) <= 0.2 then
                b.source.autorangev = b.AUTORANGE_OFF
                b.source.rangev = 0.2
            else
                b.source.autorangev = b.AUTORANGE_ON
            end
            b.trigger.source.limiti = 0
            b.source.highc = b.DISABLE
            b.source.offmode = b.OUTPUT_NORMAL
            if blim ~= nil and blim > 0 then
                b.source.limiti = blim
            end
            b.source.levelv = blev
            b.trigger.source.linearv(blev, blev, 10)
        end
        b.source.delay = 0
        b.measure.delay = 0
        b.trigger.source.action = b.ENABLE
        b.trigger.count = n
        b.trigger.arm.count = 1
        b.trigger.arm.stimulus = 0
        b.trigger.source.stimulus = trigger.blender[1].EVENT_ID
        trigger.blender[2].stimulus[2] = b.trigger.SOURCE_COMPLETE_EVENT_ID
        trigger.blender[3].stimulus[2] = b.trigger.PULSE_COMPLETE_EVENT_ID
        if measure_bias ~= 0 then
            b.trigger.measure.iv(b.nvbuffer1, b.nvbuffer2)
            b.trigger.measure.action = b.ENABLE
            b.trigger.measure.stimulus = trigger.blender[2].EVENT_ID
            b.trigger.endpulse.action = b.SOURCE_HOLD
            b.trigger.endpulse.stimulus = b.trigger.MEASURE_COMPLETE_EVENT_ID
            b.trigger.endsweep.action = b.SOURCE_HOLD
        else
            b.trigger.measure.action = b.DISABLE
        end
        b.source.output = b.OUTPUT_ON
    end

    if b ~= nil then
        b.trigger.initiate()
    end
    c.source.output = c.OUTPUT_ON
    c.trigger.initiate()
end

function wait_pulse_done()
    waitcomplete()
end

function configure_pulse_timeline(cn, on, smode, levels, delays, n, lim, bmode, blev, blim, measure_bias)
    local c = _G[cn]
    local b = nil
    if on ~= nil then
        b = _G[on]
    end
    if c == nil then
        error("Unknown pulse SMU: " .. tostring(cn))
    end

    c.abort()
    if b ~= nil then
        b.abort()
    end
    trigger.blender[1].reset()
    trigger.timer[2].reset()

    _pulse_pb(c)
    if b ~= nil then
        _pulse_pb(b)
    end

    if smode == 1 then
        c.source.func = c.OUTPUT_DCAMPS
        if lim ~= nil and lim > 0 then
            c.source.limitv = lim
        end
        c.source.leveli = 0
        c.trigger.source.listi(levels)
    else
        c.source.func = c.OUTPUT_DCVOLTS
        if lim ~= nil and lim > 0 then
            c.source.limiti = lim
        end
        c.source.levelv = 0
        c.trigger.source.listv(levels)
    end

    c.source.delay = 0
    c.measure.delay = 0
    c.trigger.source.action = c.ENABLE
    c.trigger.measure.iv(c.nvbuffer1, c.nvbuffer2)
    c.trigger.measure.action = c.ENABLE
    c.trigger.count = n
    c.trigger.arm.count = 1
    c.trigger.arm.stimulus = 0
    c.trigger.measure.stimulus = c.trigger.SOURCE_COMPLETE_EVENT_ID

    if n > 1 then
        trigger.timer[2].delaylist = delays
        trigger.timer[2].count = n
        trigger.timer[2].passthrough = false
        trigger.timer[2].stimulus = c.trigger.SOURCE_COMPLETE_EVENT_ID

        trigger.blender[1].orenable = true
        trigger.blender[1].stimulus[1] = c.trigger.ARMED_EVENT_ID
        trigger.blender[1].stimulus[2] = c.trigger.PULSE_COMPLETE_EVENT_ID
        c.trigger.source.stimulus = trigger.blender[1].EVENT_ID
        c.trigger.endpulse.stimulus = trigger.timer[2].EVENT_ID
    else
        c.trigger.source.stimulus = 0
        c.trigger.endpulse.stimulus = c.trigger.MEASURE_COMPLETE_EVENT_ID
    end

    c.trigger.endpulse.action = c.SOURCE_HOLD
    c.trigger.endsweep.action = c.SOURCE_IDLE

    if b ~= nil then
        if bmode == 1 then
            b.source.func = b.OUTPUT_DCAMPS
            if blim ~= nil and blim > 0 then
                b.source.limitv = blim
            end
            b.source.leveli = blev
        else
            b.source.func = b.OUTPUT_DCVOLTS
            if blim ~= nil and blim > 0 then
                b.source.limiti = blim
            end
            b.source.levelv = blev
        end
        b.source.delay = 0
        b.measure.delay = 0
        b.trigger.source.action = b.DISABLE
        if measure_bias ~= 0 then
            b.trigger.measure.iv(b.nvbuffer1, b.nvbuffer2)
            b.trigger.measure.action = b.ENABLE
            b.trigger.count = n
            b.trigger.arm.count = 1
            b.trigger.arm.stimulus = 0
            b.trigger.measure.stimulus = c.trigger.SOURCE_COMPLETE_EVENT_ID
            b.trigger.endpulse.action = b.SOURCE_HOLD
            b.trigger.endpulse.stimulus = b.trigger.MEASURE_COMPLETE_EVENT_ID
            b.trigger.endsweep.action = b.SOURCE_HOLD
        else
            b.trigger.measure.action = b.DISABLE
        end
        b.source.output = b.OUTPUT_ON
    end

    if b ~= nil and measure_bias ~= 0 then
        b.trigger.initiate()
    end
    c.source.output = c.OUTPUT_ON
    c.trigger.initiate()
end
        """

    def _ensure_pulse_script_loaded(self) -> None:
        if self._pulse_script_loaded:
            return
        if self.debug:
            self._pulse_script_loaded = True
            return
        self._load_tsp_script_with_functions(
            self.PULSE_SCRIPT_NAME,
            self._build_pulse_script(),
            [
                "configure_pulse",
                "configure_pulse_list_sweep",
                "configure_pulse_timeline",
                "wait_pulse_done",
            ],
        )
        self._pulse_script_loaded = True

    def run_pulse_sequence(
        self,
        smu_channel: str,
        source_mode: str,
        events: list[PulseEvent],
        source_limit: float | None = None,
        measurement_items: list[str] | None = None,
        stop_checker: Callable[[], bool] | None = None,
    ) -> Generator[
        tuple[
            list[float],
            list[float] | None,
            list[float] | None,
            list[float],
        ],
        None,
        None,
    ]:
        if not events:
            return

        self._ensure_ascii_stream_format()
        self._ensure_pulse_script_loaded()
        self._raise_if_error_queue("preparing pulse sequence")

        try:
            self._clear_error_state()
            active_channel = smu_channel
            inactive_channel = "smub" if smu_channel == "smua" else "smua"
            normalized_mode = str(source_mode or "voltage").strip().lower()
            source_mode_token = 1 if normalized_mode == "current" else 0
            limit = source_limit
            if limit is None:
                limit = self._source_limits.get(active_channel)

            levels = [float(event.level) for event in events]
            widths = [float(event.width_s) for event in events]
            periods = [float(event.period_s) for event in events]
            total_points = len(events)

            def _abort_if_requested() -> None:
                if stop_checker is None or not stop_checker():
                    return
                self.abort_sweep()
                raise InterruptedError("Pulse run aborted by user.")

            def _debug_pulse_data() -> tuple[
                list[float],
                list[float] | None,
                list[float] | None,
                list[float],
            ]:
                timestamps: list[float] = []
                elapsed_s = 0.0
                for event in events:
                    timestamps.append(elapsed_s)
                    elapsed_s += max(float(event.period_s), 0.0)
                if source_mode_token == 1:
                    currents = list(levels)
                    voltages = [
                        0.05 + level * 1.0e5 + index * 1e-4
                        for index, level in enumerate(levels)
                    ]
                else:
                    voltages = list(levels)
                    currents = [
                        1.23e-6 + level * 1e-7 + index * 1e-9
                        for index, level in enumerate(levels)
                    ]
                return levels, currents, voltages, timestamps

            if self.debug:
                time.sleep(self.DEFAULT_POLL_INTERVAL_S)
                yield _debug_pulse_data()
                self._source_levels[active_channel] = 0.0
                self._source_levels[inactive_channel] = 0.0
                return

            self._load_tsp_numeric_table("oai_pulse_levels", levels)
            self._load_tsp_numeric_table("oai_pulse_widths", widths)
            self._load_tsp_numeric_table("oai_pulse_periods", periods)
            self._send_cmd_checked(
                "configure_pulse("
                f"{self._format_tsp_value(active_channel)}, "
                f"{self._format_tsp_value(inactive_channel)}, "
                f"{source_mode_token}, "
                "oai_pulse_levels, "
                "oai_pulse_widths, "
                "oai_pulse_periods, "
                f"{total_points}, "
                f"{self._format_tsp_value(limit)})",
                f"starting pulse sequence on {active_channel}",
            )

            old_n = 0
            while old_n < total_points:
                time.sleep(self.DEFAULT_POLL_INTERVAL_S)
                _abort_if_requested()
                self._raise_if_error_queue(f"polling pulse progress on {active_channel}")
                current_n = int(
                    float(
                        self._query_cmd_checked(
                            f"print({active_channel}.nvbuffer1.n)",
                            f"reading pulse buffer count on {active_channel}",
                        )
                    )
                )
                current_n = min(current_n, total_points)
                if current_n <= old_n:
                    continue
                if (
                    current_n < total_points
                    and current_n - old_n < self.MIN_CHUNK_POINTS
                ):
                    continue

                pull_start = old_n + 1
                while pull_start <= current_n:
                    _abort_if_requested()
                    pull_end = min(
                        pull_start + self.MAX_POINTS_PER_PRINTBUFFER - 1,
                        current_n,
                    )
                    reply = self._query_cmd_checked(
                        (
                            f"printbuffer({pull_start}, {pull_end}, "
                            f"{active_channel}.nvbuffer1.readings, "
                            f"{active_channel}.nvbuffer1.timestamps)"
                        ),
                        f"reading pulse buffer rows {pull_start}-{pull_end}",
                    )
                    rows = self._reshape_printbuffer_rows(reply, 2)
                    if rows:
                        chunk_levels = levels[pull_start - 1 : pull_start - 1 + len(rows)]
                        readings = [row[0] for row in rows]
                        timestamps = [row[1] for row in rows]
                        if source_mode_token == 1:
                            currents = list(chunk_levels)
                            voltages = readings
                        else:
                            currents = readings
                            voltages = list(chunk_levels)
                        yield (chunk_levels, currents, voltages, timestamps)
                    pull_start = pull_end + 1

                old_n = current_n

            _abort_if_requested()
            self._query_cmd_checked(
                "wait_pulse_done() print(1)",
                "waiting for pulse sequence completion",
            )
            self._source_levels[active_channel] = 0.0
            self._source_levels[inactive_channel] = 0.0
        except Exception:
            self._recover_from_sweep_error()
            raise

    def run_pulse_timeline(
        self,
        smu_channel: str,
        source_mode: str,
        timeline: list[PulseTimelinePoint],
        source_limit: float | None = None,
        measurement_items: list[str] | None = None,
        bias_config: dict | None = None,
        stop_checker: Callable[[], bool] | None = None,
    ) -> Generator[
        tuple[
            list[float],
            list[float] | None,
            list[float] | None,
            list[float] | None,
            list[float] | None,
            list[float] | None,
            list[float],
        ],
        None,
        None,
    ]:
        if not timeline:
            return

        self._ensure_ascii_stream_format()
        self._ensure_pulse_script_loaded()
        self._raise_if_error_queue("preparing pulse timeline")

        try:
            self._clear_error_state()
            active_channel = smu_channel
            normalized_mode = str(source_mode or "voltage").strip().lower()
            source_mode_token = 1 if normalized_mode == "current" else 0
            limit = source_limit
            if limit is None:
                limit = self._source_limits.get(active_channel)

            levels = [float(point.source_level) for point in timeline]
            delays = [float(point.dwell_to_next_s) for point in timeline[:-1]]
            delays.append(self.TSP_FINAL_ENDPULSE_DELAY_S)
            timestamps = [float(point.time_s) for point in timeline]
            total_points = len(timeline)

            bias_channel: str | None = None
            bias_mode_token = 0
            bias_level = 0.0
            bias_limit: float | None = None
            measure_bias = False
            if isinstance(bias_config, dict):
                bias_channel = str(bias_config.get("channel", "")).strip() or None
                bias_mode = str(bias_config.get("source_mode", "voltage")).strip().lower()
                bias_mode_token = 1 if bias_mode == "current" else 0
                bias_level = float(bias_config.get("level", 0.0))
                bias_limit = float(bias_config.get("limit", 0.0))
                measure_bias = bias_channel is not None

            self._source_modes[active_channel] = normalized_mode
            if bias_channel is not None:
                self._source_modes[bias_channel] = (
                    "current" if bias_mode_token == 1 else "voltage"
                )

            def _abort_if_requested() -> None:
                if stop_checker is None or not stop_checker():
                    return
                self.abort_sweep()
                raise InterruptedError("Pulse run aborted by user.")

            def _debug_pulse_timeline_data() -> tuple[
                list[float],
                list[float] | None,
                list[float] | None,
                list[float] | None,
                list[float] | None,
                list[float] | None,
                list[float],
            ]:
                if source_mode_token == 1:
                    active_currents = list(levels)
                    active_voltages = [
                        0.05 + level * 1.0e5 + index * 1e-4
                        for index, level in enumerate(levels)
                    ]
                else:
                    active_voltages = list(levels)
                    active_currents = [
                        1.23e-6 + level * 1e-7 + index * 1e-9
                        for index, level in enumerate(levels)
                    ]

                bias_sources: list[float] | None = None
                bias_currents: list[float] | None = None
                bias_voltages: list[float] | None = None
                if bias_channel is not None:
                    bias_sources = [bias_level] * len(levels)
                    if bias_mode_token == 1:
                        bias_currents = list(bias_sources)
                        bias_voltages = [
                            0.02 + bias_level * 1.0e5 + index * 5e-5
                            for index in range(len(levels))
                        ]
                    else:
                        bias_voltages = list(bias_sources)
                        bias_currents = [
                            8.9e-7 + bias_level * 8e-8 + index * 1e-9
                            for index in range(len(levels))
                        ]
                return (
                    levels,
                    active_currents,
                    active_voltages,
                    bias_sources,
                    bias_currents,
                    bias_voltages,
                    timestamps,
                )

            if self.debug:
                time.sleep(self.DEFAULT_POLL_INTERVAL_S)
                yield _debug_pulse_timeline_data()
                self._source_levels[active_channel] = 0.0
                if bias_channel is not None:
                    self._source_levels[bias_channel] = bias_level
                return

            self._load_tsp_numeric_table("oai_pulse_levels", levels)
            self._load_tsp_numeric_table("oai_pulse_delays", delays)
            self._send_cmd_checked(
                "configure_pulse_timeline("
                f"{self._format_tsp_value(active_channel)}, "
                f"{self._format_tsp_value(bias_channel)}, "
                f"{source_mode_token}, "
                "oai_pulse_levels, "
                "oai_pulse_delays, "
                f"{total_points}, "
                f"{self._format_tsp_value(limit)}, "
                f"{bias_mode_token}, "
                f"{self._format_tsp_value(bias_level)}, "
                f"{self._format_tsp_value(bias_limit)}, "
                f"{1 if measure_bias else 0})",
                f"starting pulse timeline on {active_channel}",
            )

            old_n = 0
            while old_n < total_points:
                time.sleep(self.DEFAULT_POLL_INTERVAL_S)
                _abort_if_requested()
                self._raise_if_error_queue(f"polling pulse timeline on {active_channel}")
                active_n = int(
                    float(
                        self._query_cmd_checked(
                            f"print({active_channel}.nvbuffer1.n)",
                            f"reading pulse buffer count on {active_channel}",
                        )
                    )
                )
                current_n = min(active_n, total_points)
                if measure_bias and bias_channel is not None:
                    bias_n = int(
                        float(
                            self._query_cmd_checked(
                                f"print({bias_channel}.nvbuffer1.n)",
                                f"reading pulse buffer count on {bias_channel}",
                            )
                        )
                    )
                    current_n = min(current_n, bias_n)
                if current_n <= old_n:
                    continue
                if (
                    current_n < total_points
                    and current_n - old_n < self.MIN_CHUNK_POINTS
                ):
                    continue

                pull_start = old_n + 1
                while pull_start <= current_n:
                    _abort_if_requested()
                    pull_end = min(
                        pull_start + self.MAX_POINTS_PER_PRINTBUFFER - 1,
                        current_n,
                    )
                    columns = [
                        f"{active_channel}.nvbuffer1.readings",
                        f"{active_channel}.nvbuffer1.timestamps",
                        f"{active_channel}.nvbuffer2.readings",
                    ]
                    bias_current_index: int | None = None
                    bias_voltage_index: int | None = None
                    if measure_bias and bias_channel is not None:
                        bias_current_index = len(columns)
                        columns.append(f"{bias_channel}.nvbuffer1.readings")
                        bias_voltage_index = len(columns)
                        columns.append(f"{bias_channel}.nvbuffer2.readings")

                    reply = self._query_cmd_checked(
                        f"printbuffer({pull_start}, {pull_end}, {', '.join(columns)})",
                        f"reading pulse timeline rows {pull_start}-{pull_end}",
                    )
                    rows = self._reshape_printbuffer_rows(reply, len(columns))
                    if rows:
                        chunk_levels = levels[pull_start - 1 : pull_start - 1 + len(rows)]
                        active_currents = [row[0] for row in rows]
                        active_timestamps = [row[1] for row in rows]
                        active_voltages = [row[2] for row in rows]
                        bias_sources = None
                        bias_currents = None
                        bias_voltages = None
                        if (
                            bias_current_index is not None
                            and bias_voltage_index is not None
                        ):
                            bias_sources = [bias_level] * len(rows)
                            bias_currents = [row[bias_current_index] for row in rows]
                            bias_voltages = [row[bias_voltage_index] for row in rows]
                        yield (
                            chunk_levels,
                            active_currents,
                            active_voltages,
                            bias_sources,
                            bias_currents,
                            bias_voltages,
                            active_timestamps,
                        )
                    pull_start = pull_end + 1

                old_n = current_n

            _abort_if_requested()
            self._query_cmd_checked(
                "wait_pulse_done() print(1)",
                "waiting for pulse timeline completion",
            )
            self._source_levels[active_channel] = 0.0
            if bias_channel is not None:
                self._source_levels[bias_channel] = bias_level
        except Exception:
            self._recover_from_sweep_error()
            raise

    def run_pulse_list_sweep(
        self,
        smu_channel: str,
        source_mode: str,
        source_levels: list[float],
        src_to_meas_delay_s: float,
        nplc: float,
        source_limit: float | None = None,
        measurement_items: list[str] | None = None,
        bias_config: dict | None = None,
        stop_checker: Callable[[], bool] | None = None,
    ) -> Generator[
        tuple[
            list[float],
            list[float] | None,
            list[float] | None,
            list[float] | None,
            list[float] | None,
            list[float] | None,
            list[float],
        ],
        None,
        None,
    ]:
        if callable(bias_config) and stop_checker is None:
            stop_checker = bias_config
            bias_config = measurement_items if isinstance(measurement_items, dict) else None
            measurement_items = (
                list(source_limit)
                if isinstance(source_limit, list)
                else None
            )
            source_limit = float(nplc) if isinstance(nplc, (int, float)) else None
            nplc = float(self._measure_state.get(smu_channel, {}).get("nplc", 1.0))

        if not source_levels:
            return

        self._ensure_ascii_stream_format()
        self._ensure_pulse_script_loaded()
        self._raise_if_error_queue("preparing pulse list sweep")

        try:
            self._clear_error_state()
            active_channel = smu_channel
            normalized_mode = str(source_mode or "voltage").strip().lower()
            source_mode_token = 1 if normalized_mode == "current" else 0
            limit = source_limit
            if limit is None:
                limit = self._source_limits.get(active_channel)

            levels = [float(level) for level in source_levels]
            total_points = len(levels)
            delay_s = max(float(src_to_meas_delay_s), 0.0)
            effective_nplc = max(float(nplc), 0.0)

            bias_channel: str | None = None
            bias_mode_token = 0
            bias_level = 0.0
            bias_limit: float | None = None
            measure_bias = False
            if isinstance(bias_config, dict):
                unsupported_mode = str(
                    bias_config.get("mode", bias_config.get("sweep_mode", "fixed"))
                ).strip().lower()
                if unsupported_mode not in {"", "fixed", "constant"}:
                    raise ValueError(
                        "Pulse list sweep currently supports only fixed bias mode."
                    )
                bias_channel = str(bias_config.get("channel", "")).strip() or None
                bias_mode = str(bias_config.get("source_mode", "voltage")).strip().lower()
                bias_mode_token = 1 if bias_mode == "current" else 0
                bias_level = float(bias_config.get("level", 0.0))
                bias_limit = float(bias_config.get("limit", 0.0))
                measure_bias = bias_channel is not None

            self._source_modes[active_channel] = normalized_mode
            if bias_channel is not None:
                self._source_modes[bias_channel] = (
                    "current" if bias_mode_token == 1 else "voltage"
                )

            def _abort_if_requested() -> None:
                if stop_checker is None or not stop_checker():
                    return
                self.abort_sweep()
                raise InterruptedError("Pulse run aborted by user.")

            def _configure_list_measurement(channel: str) -> None:
                self._send_cmd_checked(
                    f"{channel}.measure.nplc = {effective_nplc}",
                    f"setting pulse list sweep NPLC on {channel}",
                )
                self._send_cmd_checked(
                    f"{channel}.measure.autozero = {channel}.AUTOZERO_OFF",
                    f"disabling pulse list sweep autozero on {channel}",
                )
                self._send_cmd_checked(
                    f"{channel}.measure.interval = 0",
                    f"setting pulse list sweep measure interval on {channel}",
                )
                self._send_cmd_checked(
                    f"{channel}.measure.delay = 0",
                    f"setting pulse list sweep measure delay on {channel}",
                )
                self._send_cmd_checked(
                    f"{channel}.measure.count = 1",
                    f"setting pulse list sweep measure count on {channel}",
                )
                self._send_cmd_checked(
                    f"{channel}.measure.rel.leveli = 0",
                    f"setting pulse list sweep relative current level on {channel}",
                )
                self._send_cmd_checked(
                    f"{channel}.measure.filter.count = 10",
                    f"setting pulse list sweep filter count on {channel}",
                )
                self._send_cmd_checked(
                    f"{channel}.measure.filter.enable = {channel}.FILTER_OFF",
                    f"disabling pulse list sweep filter on {channel}",
                )
                self._send_cmd_checked(
                    f"{channel}.measure.lowrangei = 1E-10",
                    f"setting pulse list sweep low current range on {channel}",
                )
                self._send_cmd_checked(
                    f"{channel}.sense = {channel}.SENSE_LOCAL",
                    f"setting pulse list sweep local sense on {channel}",
                )
                self._apply_current_measure_range(channel)

            def _debug_pulse_list_data() -> tuple[
                list[float],
                list[float] | None,
                list[float] | None,
                list[float] | None,
                list[float] | None,
                list[float] | None,
                list[float],
            ]:
                point_interval_s = effective_nplc / 50.0 + delay_s
                timestamps = [
                    float(index) * point_interval_s for index in range(len(levels))
                ]
                if source_mode_token == 1:
                    active_currents = list(levels)
                    active_voltages = [
                        0.05 + level * 1.0e5 + index * 1e-4
                        for index, level in enumerate(levels)
                    ]
                else:
                    active_voltages = list(levels)
                    active_currents = [
                        1.23e-6 + level * 1e-7 + index * 1e-9
                        for index, level in enumerate(levels)
                    ]

                bias_sources: list[float] | None = None
                bias_currents: list[float] | None = None
                bias_voltages: list[float] | None = None
                if bias_channel is not None:
                    bias_sources = [bias_level] * len(levels)
                    if bias_mode_token == 1:
                        bias_currents = list(bias_sources)
                        bias_voltages = [
                            0.02 + bias_level * 1.0e5 + index * 5e-5
                            for index in range(len(levels))
                        ]
                    else:
                        bias_voltages = list(bias_sources)
                        bias_currents = [
                            8.9e-7 + bias_level * 8e-8 + index * 1e-9
                            for index in range(len(levels))
                        ]
                return (
                    levels,
                    active_currents,
                    active_voltages,
                    bias_sources,
                    bias_currents,
                    bias_voltages,
                    timestamps,
                )

            if self.debug:
                time.sleep(self.DEFAULT_POLL_INTERVAL_S)
                yield _debug_pulse_list_data()
                self._source_levels[active_channel] = 0.0
                if bias_channel is not None:
                    self._source_levels[bias_channel] = bias_level
                return

            _configure_list_measurement(active_channel)
            if bias_channel is not None:
                _configure_list_measurement(bias_channel)

            self._load_tsp_numeric_table("oai_pulse_levels", levels)
            self._send_cmd_checked(
                "configure_pulse_list_sweep("
                f"{self._format_tsp_value(active_channel)}, "
                f"{self._format_tsp_value(bias_channel)}, "
                f"{source_mode_token}, "
                "oai_pulse_levels, "
                f"{total_points}, "
                f"{self._format_tsp_value(delay_s)}, "
                f"{self._format_tsp_value(limit)}, "
                f"{bias_mode_token}, "
                f"{self._format_tsp_value(bias_level)}, "
                f"{self._format_tsp_value(bias_limit)}, "
                f"{1 if measure_bias else 0})",
                f"starting pulse list sweep on {active_channel}",
            )

            old_n = 0
            while old_n < total_points:
                time.sleep(self.DEFAULT_POLL_INTERVAL_S)
                _abort_if_requested()
                self._raise_if_error_queue(f"polling pulse list sweep on {active_channel}")
                active_n = int(
                    float(
                        self._query_cmd_checked(
                            f"print({active_channel}.nvbuffer1.n)",
                            f"reading pulse list buffer count on {active_channel}",
                        )
                    )
                )
                current_n = min(active_n, total_points)
                if measure_bias and bias_channel is not None:
                    bias_n = int(
                        float(
                            self._query_cmd_checked(
                                f"print({bias_channel}.nvbuffer1.n)",
                                f"reading pulse list buffer count on {bias_channel}",
                            )
                        )
                    )
                    current_n = min(current_n, bias_n)
                if current_n <= old_n:
                    continue
                if (
                    current_n < total_points
                    and current_n - old_n < self.MIN_CHUNK_POINTS
                ):
                    continue

                pull_start = old_n + 1
                while pull_start <= current_n:
                    _abort_if_requested()
                    pull_end = min(
                        pull_start + self.MAX_POINTS_PER_PRINTBUFFER - 1,
                        current_n,
                    )
                    columns = [
                        f"{active_channel}.nvbuffer1.readings",
                        f"{active_channel}.nvbuffer1.timestamps",
                        f"{active_channel}.nvbuffer2.readings",
                    ]
                    bias_current_index: int | None = None
                    bias_voltage_index: int | None = None
                    if measure_bias and bias_channel is not None:
                        bias_current_index = len(columns)
                        columns.append(f"{bias_channel}.nvbuffer1.readings")
                        bias_voltage_index = len(columns)
                        columns.append(f"{bias_channel}.nvbuffer2.readings")

                    reply = self._query_cmd_checked(
                        f"printbuffer({pull_start}, {pull_end}, {', '.join(columns)})",
                        f"reading pulse list rows {pull_start}-{pull_end}",
                    )
                    rows = self._reshape_printbuffer_rows(reply, len(columns))
                    if rows:
                        chunk_levels = levels[pull_start - 1 : pull_start - 1 + len(rows)]
                        active_currents = [row[0] for row in rows]
                        active_timestamps = [row[1] for row in rows]
                        active_voltages = [row[2] for row in rows]
                        bias_sources = None
                        bias_currents = None
                        bias_voltages = None
                        if (
                            bias_current_index is not None
                            and bias_voltage_index is not None
                        ):
                            bias_sources = [bias_level] * len(rows)
                            bias_currents = [row[bias_current_index] for row in rows]
                            bias_voltages = [row[bias_voltage_index] for row in rows]
                        yield (
                            chunk_levels,
                            active_currents,
                            active_voltages,
                            bias_sources,
                            bias_currents,
                            bias_voltages,
                            active_timestamps,
                        )
                    pull_start = pull_end + 1

                old_n = current_n

            _abort_if_requested()
            self._query_cmd_checked(
                "wait_pulse_done() print(1)",
                "waiting for pulse list sweep completion",
            )
            self._source_levels[active_channel] = 0.0
            if bias_channel is not None:
                self._source_levels[bias_channel] = bias_level
        except Exception:
            self._recover_from_sweep_error()
            raise

    def run_single_smu_sweep(
        self,
        smu_channel: str,
        start_v: float,
        stop_v: float,
        points: int,
        delay: float = 0.0,
        nplc: float = 1.0,
        current_limit: float | None = None,
        measurement_items: list[str] | None = None,
        ramp_up: bool = False,
        ru_step: float = 0.5,
        ru_delay: float = 0.1,
        ramp_down: bool = False,
        rd_step: float = 0.5,
        rd_delay: float = 0.1,
        stop_checker: Callable[[], bool] | None = None,
    ) -> Generator[
        tuple[
            list[float],
            list[float],
            list[float] | None,
            list[float],
        ],
        None,
        None,
    ]:
        if points < 1:
            return

        self._ensure_ascii_stream_format()
        self._ensure_dual_sync_script_loaded()
        self._raise_if_error_queue("preparing single-SMU sweep")

        try:
            self._clear_error_state()
            active_channel = smu_channel
            inactive_channel = "smub" if smu_channel == "smua" else "smua"
            sweep_current_limit = current_limit
            if sweep_current_limit is None:
                sweep_current_limit = self._current_limits.get(active_channel)

            requested_measurements = set(measurement_items or [])
            capture_voltage = bool({"Voltage", "Resistance"} & requested_measurements)

            def _abort_if_requested() -> None:
                if stop_checker is None or not stop_checker():
                    return
                self.abort_sweep()
                raise InterruptedError("Sweep aborted by user.")

            def _configure_fast_measurement(channel: str) -> None:
                self._send_cmd_checked(
                    f"{channel}.measure.nplc = {float(nplc)}",
                    f"setting single sweep NPLC on {channel}",
                )
                self._send_cmd_checked(
                    f"{channel}.measure.filter.enable = {channel}.FILTER_OFF",
                    f"disabling single sweep filter on {channel}",
                )
                self._send_cmd_checked(
                    f"{channel}.measure.autozero = {channel}.AUTOZERO_ONCE",
                    f"arming autozero once on {channel}",
                )
                self._send_cmd_checked(
                    f"{channel}.measure.autozero = {channel}.AUTOZERO_OFF",
                    f"disabling autozero on {channel}",
                )
                self._apply_current_measure_range(channel)

            def _build_linear_segment(
                start_value: float,
                stop_value: float,
                total_points: int,
                start_index: int,
                end_index: int,
            ) -> list[float]:
                if end_index < start_index:
                    return []
                if total_points <= 1:
                    return [float(start_value)] * max(end_index - start_index + 1, 0)
                step_value = (float(stop_value) - float(start_value)) / float(total_points - 1)
                return [
                    float(start_value) + step_value * (point_index - 1)
                    for point_index in range(start_index, end_index + 1)
                ]

            def _pull_chunk(start_index: int, end_index: int) -> tuple[
                list[float],
                list[float] | None,
                list[float],
            ]:
                columns = [
                    f"{active_channel}.nvbuffer1.readings",
                    f"{active_channel}.nvbuffer1.timestamps",
                ]
                timestamp_index = 1
                voltage_index: int | None = None
                if capture_voltage:
                    voltage_index = len(columns)
                    columns.append(f"{active_channel}.nvbuffer2.readings")

                reply = self._query_cmd_checked(
                    f"printbuffer({start_index}, {end_index}, {', '.join(columns)})",
                    f"reading single sweep buffer rows {start_index}-{end_index}",
                )
                rows = self._reshape_printbuffer_rows(reply, len(columns))
                currents = [row[0] for row in rows]
                timestamps = [row[timestamp_index] for row in rows]
                measured_voltages = (
                    None
                    if voltage_index is None
                    else [row[voltage_index] for row in rows]
                )
                return currents, measured_voltages, timestamps

            def _start_block(
                block_start: float,
                block_stop: float,
                block_points: int,
                block_delay: float,
            ) -> None:
                self._send_cmd_checked(
                    "configure_single_block("
                    f"{self._format_tsp_value(active_channel)}, "
                    f"{self._format_tsp_value(inactive_channel)}, "
                    f"{self._format_tsp_value(block_start)}, "
                    f"{self._format_tsp_value(block_stop)}, "
                    f"{block_points}, "
                    f"{self._format_tsp_value(block_delay)}, "
                    f"{self._format_tsp_value(sweep_current_limit)}, "
                    f"{1 if capture_voltage else 0})",
                    (
                        f"starting single-SMU sweep block {block_start} V to "
                        f"{block_stop} V with {block_points} points"
                    ),
                )

            def _debug_block_data(
                block_start: float,
                block_stop: float,
                block_points: int,
                block_delay: float,
            ) -> tuple[list[float], list[float], list[float] | None, list[float]]:
                if block_points <= 1:
                    source_values = [float(block_start)]
                else:
                    step_v = (block_stop - block_start) / float(block_points - 1)
                    source_values = [
                        float(block_start) + step_v * index for index in range(block_points)
                    ]
                currents = [
                    1.23e-6 + (value - float(block_start)) * 1e-7
                    for value in source_values
                ]
                measured_voltages = list(source_values) if capture_voltage else None
                timestamps = [
                    float(index) * max(float(block_delay), 0.0) for index in range(block_points)
                ]
                return source_values, currents, measured_voltages, timestamps

            def _run_block(
                block_start: float,
                block_stop: float,
                block_points: int,
                block_delay: float,
            ) -> Generator[
                tuple[list[float], list[float], list[float] | None, list[float]],
                None,
                None,
            ]:
                if block_points < 1:
                    return

                if self.debug:
                    time.sleep(self.DEFAULT_POLL_INTERVAL_S)
                    yield _debug_block_data(block_start, block_stop, block_points, block_delay)
                    self._source_levels[active_channel] = float(block_stop)
                    self._source_levels[inactive_channel] = 0.0
                    return

                _start_block(block_start, block_stop, block_points, block_delay)
                block_old_n = 0
                while block_old_n < block_points:
                    time.sleep(self.DEFAULT_POLL_INTERVAL_S)
                    _abort_if_requested()
                    self._raise_if_error_queue(
                        f"polling single sweep progress on {active_channel}"
                    )
                    current_n = int(
                        float(
                            self._query_cmd_checked(
                                f"print({active_channel}.nvbuffer1.n)",
                                f"reading buffer count on {active_channel}",
                            )
                        )
                    )
                    current_n = min(current_n, block_points)
                    if current_n <= block_old_n:
                        continue
                    if (
                        current_n < block_points
                        and current_n - block_old_n < self.MIN_CHUNK_POINTS
                    ):
                        continue

                    pull_start = block_old_n + 1
                    while pull_start <= current_n:
                        _abort_if_requested()
                        pull_end = min(
                            pull_start + self.MAX_POINTS_PER_PRINTBUFFER - 1,
                            current_n,
                        )
                        currents, measured_voltages, timestamps = _pull_chunk(
                            pull_start,
                            pull_end,
                        )
                        if currents:
                            actual_pull_end = pull_start + len(currents) - 1
                            source_values = _build_linear_segment(
                                block_start,
                                block_stop,
                                block_points,
                                pull_start,
                                actual_pull_end,
                            )
                            yield (
                                source_values,
                                currents,
                                measured_voltages,
                                timestamps,
                            )
                        pull_start = pull_end + 1

                    block_old_n = current_n

                _abort_if_requested()
                self._query_cmd_checked(
                    "wait_done() print(1)",
                    "waiting for single sweep block completion",
                )
                self._source_levels[active_channel] = float(block_stop)
                self._source_levels[inactive_channel] = 0.0

            _configure_fast_measurement(active_channel)
            self._send_cmd_checked(
                f"{inactive_channel}.source.output = {inactive_channel}.OUTPUT_OFF",
                f"turning off inactive {inactive_channel}",
            )

            if ramp_up and abs(start_v) > 0:
                ramp_up_points = max(2, int(math.ceil(abs(start_v) / max(ru_step, 1e-9))) + 1)
                yield from _run_block(0.0, start_v, ramp_up_points, ru_delay)

            _abort_if_requested()
            yield from _run_block(start_v, stop_v, points, delay)

            if ramp_down and abs(stop_v) > 0:
                ramp_down_points = max(
                    2,
                    int(math.ceil(abs(stop_v) / max(rd_step, 1e-9))) + 1,
                )
                yield from _run_block(stop_v, 0.0, ramp_down_points, rd_delay)
        except Exception:
            self._recover_from_sweep_error()
            raise

    def run_iv_sweep(
        self,
        smu_channel: str,
        start_v: float,
        stop_v: float,
        points: int,
        delay: float = 0.0,
        nplc: float = 1.0,
        current_limit: float | None = None,
        measurement_items: list[str] | None = None,
        ramp_up: bool = False,
        ru_step: float = 0.5,
        ru_delay: float = 0.1,
        ramp_down: bool = False,
        rd_step: float = 0.5,
        rd_delay: float = 0.1,
        secondary_mode: str = "fixed",
        secondary_level: float = 0.0,
        secondary_start_v: float | None = None,
        secondary_stop_v: float | None = None,
        secondary_current_limit: float | None = None,
        stop_checker: Callable[[], bool] | None = None,
    ) -> Generator[
        tuple[
            list[float],
            list[float],
            list[float] | None,
            list[float],
            list[float],
            list[float] | None,
            list[float],
        ],
        None,
        None,
    ]:
        if points < 1:
            return

        self._ensure_ascii_stream_format()
        self._ensure_dual_sync_script_loaded()
        self._raise_if_error_queue("preparing sweep")

        try:
            self._clear_error_state()
            primary_channel = smu_channel
            secondary_channel = "smub" if smu_channel == "smua" else "smua"
            sweep_current_limit = current_limit
            if sweep_current_limit is None:
                sweep_current_limit = self._current_limits.get(primary_channel)
            if secondary_current_limit is None:
                secondary_current_limit = self._current_limits.get(secondary_channel)

            requested_measurements = set(measurement_items or [])
            capture_primary_voltage = bool({"Voltage", "Resistance"} & requested_measurements)
            secondary_measurements = set(
                self._measure_state.get(secondary_channel, {}).get("signature", ())
            )
            capture_secondary_voltage = bool(
                {"Voltage", "Resistance"} & secondary_measurements
            )

            normalized_secondary_mode = str(secondary_mode).strip().lower()
            if normalized_secondary_mode == "linear":
                secondary_start = (
                    secondary_start_v if secondary_start_v is not None else secondary_level
                )
                secondary_stop = (
                    secondary_stop_v if secondary_stop_v is not None else secondary_level
                )
                secondary_mode_token = 1
            else:
                secondary_start = secondary_level
                secondary_stop = secondary_level
                secondary_mode_token = 0

            def _abort_if_requested() -> None:
                if stop_checker is None or not stop_checker():
                    return
                self.abort_sweep()
                raise InterruptedError("Sweep aborted by user.")

            def _configure_fast_measurement(channel: str) -> None:
                self._send_cmd_checked(
                    f"{channel}.measure.nplc = {float(nplc)}",
                    f"setting fast sweep NPLC on {channel}",
                )
                self._send_cmd_checked(
                    f"{channel}.measure.filter.enable = {channel}.FILTER_OFF",
                    f"disabling fast sweep filter on {channel}",
                )
                self._send_cmd_checked(
                    f"{channel}.measure.autozero = {channel}.AUTOZERO_ONCE",
                    f"arming autozero once on {channel}",
                )
                self._send_cmd_checked(
                    f"{channel}.measure.autozero = {channel}.AUTOZERO_OFF",
                    f"disabling autozero on {channel}",
                )
                self._apply_current_measure_range(channel)

            def _build_linear_segment(
                start_value: float,
                stop_value: float,
                total_points: int,
                start_index: int,
                end_index: int,
            ) -> list[float]:
                if end_index < start_index:
                    return []
                if total_points <= 1:
                    return [float(start_value)] * max(end_index - start_index + 1, 0)
                step_value = (float(stop_value) - float(start_value)) / float(total_points - 1)
                return [
                    float(start_value) + step_value * (point_index - 1)
                    for point_index in range(start_index, end_index + 1)
                ]

            def _pull_chunk(start_index: int, end_index: int) -> tuple[
                list[float],
                list[float] | None,
                list[float],
                list[float] | None,
                list[float],
            ]:
                columns = [
                    f"{primary_channel}.nvbuffer1.readings",
                    f"{primary_channel}.nvbuffer1.timestamps",
                ]
                primary_timestamp_index = 1
                primary_voltage_index: int | None = None
                if capture_primary_voltage:
                    primary_voltage_index = len(columns)
                    columns.append(f"{primary_channel}.nvbuffer2.readings")

                secondary_current_index = len(columns)
                columns.append(f"{secondary_channel}.nvbuffer1.readings")

                secondary_voltage_index: int | None = None
                if capture_secondary_voltage:
                    secondary_voltage_index = len(columns)
                    columns.append(f"{secondary_channel}.nvbuffer2.readings")

                reply = self._query_cmd_checked(
                    f"printbuffer({start_index}, {end_index}, {', '.join(columns)})",
                    f"reading sweep buffer rows {start_index}-{end_index}",
                )
                rows = self._reshape_printbuffer_rows(reply, len(columns))

                primary_currents = [row[0] for row in rows]
                primary_timestamps = [row[primary_timestamp_index] for row in rows]
                primary_measured_voltages = (
                    None
                    if primary_voltage_index is None
                    else [row[primary_voltage_index] for row in rows]
                )
                secondary_currents = [row[secondary_current_index] for row in rows]
                secondary_measured_voltages = (
                    None
                    if secondary_voltage_index is None
                    else [row[secondary_voltage_index] for row in rows]
                )

                return (
                    primary_currents,
                    primary_measured_voltages,
                    secondary_currents,
                    secondary_measured_voltages,
                    primary_timestamps,
                )

            def _start_block(
                block_start: float,
                block_stop: float,
                block_points: int,
                block_delay: float,
            ) -> None:
                self._send_cmd_checked(
                    # FIX: Call the global TSP function to avoid named-script namespace collisions.
                    "configure_block("
                    f"{self._format_tsp_value(primary_channel)}, "
                    f"{self._format_tsp_value(secondary_channel)}, "
                    f"{self._format_tsp_value(block_start)}, "
                    f"{self._format_tsp_value(block_stop)}, "
                    f"{block_points}, "
                    f"{self._format_tsp_value(block_delay)}, "
                    f"{self._format_tsp_value(sweep_current_limit)}, "
                    f"{self._format_tsp_value(secondary_current_limit)}, "
                    f"{secondary_mode_token}, "
                    f"{self._format_tsp_value(secondary_level)}, "
                    f"{self._format_tsp_value(secondary_start)}, "
                    f"{self._format_tsp_value(secondary_stop)}, "
                    f"{1 if capture_primary_voltage else 0}, "
                    f"{1 if capture_secondary_voltage else 0})",
                    (
                        f"starting sweep block {block_start} V to {block_stop} V "
                        f"with {block_points} points"
                    ),
                )

            def _debug_block_data(
                block_start: float,
                block_stop: float,
                block_points: int,
                block_delay: float,
            ) -> tuple[
                list[float],
                list[float],
                list[float] | None,
                list[float],
                list[float],
                list[float] | None,
                list[float],
            ]:
                if block_points <= 1:
                    primary_source_values = [float(block_start)]
                else:
                    step_v = (block_stop - block_start) / float(block_points - 1)
                    primary_source_values = [
                        float(block_start) + step_v * index for index in range(block_points)
                    ]
                primary_currents = [
                    1.23e-6 + (value - float(block_start)) * 1e-7
                    for value in primary_source_values
                ]
                primary_measured_voltages = (
                    list(primary_source_values) if capture_primary_voltage else None
                )
                if secondary_mode_token == 1 and block_points > 1:
                    secondary_step = (secondary_stop - secondary_start) / float(block_points - 1)
                    secondary_source_values = [
                        float(secondary_start) + secondary_step * index
                        for index in range(block_points)
                    ]
                else:
                    secondary_source_values = [float(secondary_level)] * block_points
                secondary_currents = [
                    8.9e-7 + source_value * 8e-8 + index * 1e-9
                    for index, source_value in enumerate(secondary_source_values)
                ]
                secondary_measured_voltages = (
                    list(secondary_source_values) if capture_secondary_voltage else None
                )
                primary_timestamps = [
                    float(index) * max(float(block_delay), 0.0) for index in range(block_points)
                ]
                return (
                    primary_source_values,
                    primary_currents,
                    primary_measured_voltages,
                    secondary_source_values,
                    secondary_currents,
                    secondary_measured_voltages,
                    primary_timestamps,
                )

            def _run_block(
                block_start: float,
                block_stop: float,
                block_points: int,
                block_delay: float,
            ) -> Generator[
                tuple[
                    list[float],
                    list[float],
                    list[float] | None,
                    list[float],
                    list[float],
                    list[float] | None,
                    list[float],
                ],
                None,
                None,
            ]:
                if block_points < 1:
                    return

                if self.debug:
                    time.sleep(self.DEFAULT_POLL_INTERVAL_S)
                    yield _debug_block_data(block_start, block_stop, block_points, block_delay)
                    self._source_levels[primary_channel] = float(block_stop)
                    self._source_levels[secondary_channel] = float(
                        secondary_stop if secondary_mode_token == 1 else secondary_level
                    )
                    return

                _start_block(block_start, block_stop, block_points, block_delay)
                block_old_n = 0
                while block_old_n < block_points:
                    # TODO: Consider SRQ/status-model driven completion to replace polling.
                    time.sleep(self.DEFAULT_POLL_INTERVAL_S)
                    _abort_if_requested()
                    self._raise_if_error_queue(
                        f"polling sweep progress on {primary_channel}"
                    )
                    primary_n = int(
                        float(
                            self._query_cmd_checked(
                                f"print({primary_channel}.nvbuffer1.n)",
                                f"reading buffer count on {primary_channel}",
                            )
                        )
                    )
                    secondary_n = int(
                        float(
                            self._query_cmd_checked(
                                f"print({secondary_channel}.nvbuffer1.n)",
                                f"reading buffer count on {secondary_channel}",
                            )
                        )
                    )
                    current_n = min(primary_n, secondary_n, block_points)
                    if current_n <= block_old_n:
                        continue
                    if (
                        current_n < block_points
                        and current_n - block_old_n < self.MIN_CHUNK_POINTS
                    ):
                        continue

                    pull_start = block_old_n + 1
                    while pull_start <= current_n:
                        _abort_if_requested()
                        # FIX: Cap each printbuffer request so the Keithley output queue cannot overflow.
                        pull_end = min(
                            pull_start + self.MAX_POINTS_PER_PRINTBUFFER - 1,
                            current_n,
                        )
                        (
                            primary_currents,
                            primary_measured_voltages,
                            secondary_currents,
                            secondary_measured_voltages,
                            primary_timestamps,
                        ) = _pull_chunk(pull_start, pull_end)
                        if primary_currents:
                            actual_pull_end = pull_start + len(primary_currents) - 1
                            primary_source_values = _build_linear_segment(
                                block_start,
                                block_stop,
                                block_points,
                                pull_start,
                                actual_pull_end,
                            )
                            if secondary_mode_token == 1:
                                secondary_source_values = _build_linear_segment(
                                    secondary_start,
                                    secondary_stop,
                                    block_points,
                                    pull_start,
                                    actual_pull_end,
                                )
                            else:
                                secondary_source_values = [float(secondary_level)] * len(
                                    primary_currents
                                )

                            yield (
                                primary_source_values,
                                primary_currents,
                                primary_measured_voltages,
                                secondary_source_values,
                                secondary_currents,
                                secondary_measured_voltages,
                                primary_timestamps,
                            )

                        pull_start = pull_end + 1

                    block_old_n = current_n

                _abort_if_requested()
                self._query_cmd_checked(
                    # FIX: Call the global completion helper to avoid named-script namespace collisions.
                    "wait_done() print(1)",
                    "waiting for sweep block completion",
                )
                self._source_levels[primary_channel] = float(block_stop)
                self._source_levels[secondary_channel] = float(
                    secondary_stop if secondary_mode_token == 1 else secondary_level
                )

            _configure_fast_measurement(primary_channel)
            _configure_fast_measurement(secondary_channel)

            if ramp_up and abs(start_v) > 0:
                ramp_up_points = max(2, int(math.ceil(abs(start_v) / max(ru_step, 1e-9))) + 1)
                yield from _run_block(0.0, start_v, ramp_up_points, ru_delay)

            _abort_if_requested()
            yield from _run_block(start_v, stop_v, points, delay)

            if ramp_down and abs(stop_v) > 0:
                ramp_down_points = max(
                    2,
                    int(math.ceil(abs(stop_v) / max(rd_step, 1e-9))) + 1,
                )
                yield from _run_block(stop_v, 0.0, ramp_down_points, rd_delay)
        except Exception:
            # FIX: Recover both SMUs and clear queued errors so the next sweep starts cleanly.
            self._recover_from_sweep_error()
            raise
