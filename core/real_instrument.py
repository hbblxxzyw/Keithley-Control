"""
Real instrument driver module.

Implements Keithley 2636/2600B SourceMeter control via PyVISA using TSP commands.
Supports a debug "dry run" mode that requires no physical connection.
Uses the instrument's Trigger Model for high-speed hardware scanning and
chunked data streaming.
"""

import math
import re
import time
from collections.abc import Callable, Generator

import pyvisa

from core.instrument_base import AbstractSMU


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

    def __init__(self, debug: bool = False) -> None:
        self.debug = debug
        self._rm = pyvisa.ResourceManager()
        self._resource = None
        self._current_limits: dict[str, float] = {}
        self._source_levels: dict[str, float] = {"smua": 0.0, "smub": 0.0}
        self._source_limits: dict[str, float | None] = {"smua": None, "smub": None}
        self._source_modes: dict[str, str] = {}
        self._measure_state: dict[str, dict[str, object]] = {}
        self._instrument_model = "2636B" if debug else None
        self._dual_sync_script_loaded = bool(debug)

    def _send_cmd(self, cmd: str) -> None:
        if self.debug:
            print(f"[DEBUG SEND] {cmd}")
            return
        if self._resource is None:
            raise RuntimeError("Instrument not connected; call connect() first.")
        self._resource.write(cmd)

    def _query_cmd(self, cmd: str) -> str:
        if self.debug:
            return "1.23e-6"
        if self._resource is None:
            raise RuntimeError("Instrument not connected; call connect() first.")
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

    def _load_tsp_script(self, script_name: str, script_body: str) -> None:
        # FIX: Use `loadandrunscript` so the named script is compiled and executed immediately.
        if self.debug:
            print(f"[DEBUG LOADSCRIPT] {script_name}")
            print(script_body.strip())
            return
        if self._resource is None:
            raise RuntimeError("Instrument not connected; call connect() first.")
        # FIX: Clear stale parser/runtime errors before attempting to load a script.
        self._send_cmd("*CLS")
        self._send_cmd("errorqueue.clear()")
        normalized_body = script_body.strip("\r\n")
        full_script = f"loadandrunscript {script_name}\n{normalized_body}\nendscript"
        # FIX: Use the underlying VISA resource directly so the entire script block is written once.
        self._resource.write(full_script)
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

    def _error_queue_count(self) -> int:
        if self.debug:
            return 0
        return int(float(self._query_cmd("print(errorqueue.count)")))

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
        self._send_cmd(cmd)
        self._raise_if_error_queue(context)

    def _query_cmd_checked(self, cmd: str, context: str) -> str:
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

    def connect(self, resource_str: str) -> bool:
        if self.debug:
            print(f"[DEBUG] Virtual connection OK: {resource_str}")
            return True
        try:
            self._resource = self._rm.open_resource(resource_str)
            self._resource.timeout = self.DEFAULT_TIMEOUT_MS
            self._resource.encoding = self.DEFAULT_RESOURCE_ENCODING
            self._resource.read_termination = "\n"
            self._resource.write_termination = "\n"

            if "ASRL" in resource_str.upper() or "COM" in resource_str.upper():
                self._resource.baud_rate = 57600

            self._ensure_ascii_stream_format()
            self._dual_sync_script_loaded = False
            return True
        except Exception as exc:
            print(f"Connection error: {exc}")
            return False

    def disconnect(self) -> None:
        if self.debug:
            print("[DEBUG] Virtual disconnect")
            return
        if self._resource is not None:
            self._resource.close()
            self._resource = None
        self._instrument_model = None
        self._dual_sync_script_loaded = False

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
        try:
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

    def configure_measurement(
        self,
        smu_channel: str,
        measurement_items: list[str],
        current_range: str,
        autozero: str,
        nplc: float,
    ) -> None:
        if not measurement_items:
            return

        self._measure_state[smu_channel] = {
            "count": 0,
            "signature": tuple(measurement_items),
            "last": {},
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

        range_amps = self._measurement_range_amps(current_range)
        if range_amps is None:
            self._send_cmd_checked(
                f"{smu_channel}.measure.autorangei = {smu_channel}.AUTORANGE_ON",
                f"enabling {smu_channel} current autorange",
            )
        else:
            self._send_cmd_checked(
                f"{smu_channel}.measure.autorangei = {smu_channel}.AUTORANGE_OFF",
                f"disabling {smu_channel} current autorange",
            )
            self._send_cmd_checked(
                f"{smu_channel}.measure.rangei = {range_amps}",
                f"setting {smu_channel} current range",
            )

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

            def _configure_fast_measurement(channel: str, limit_amps: float | None) -> None:
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
                if limit_amps is not None and limit_amps > 0:
                    self._send_cmd_checked(
                        f"{channel}.measure.autorangei = {channel}.AUTORANGE_OFF",
                        f"disabling current autorange during single sweep on {channel}",
                    )
                    self._send_cmd_checked(
                        f"{channel}.measure.rangei = {abs(limit_amps)}",
                        f"setting single sweep current range on {channel}",
                    )
                else:
                    self._send_cmd_checked(
                        f"{channel}.measure.autorangei = {channel}.AUTORANGE_ON",
                        f"enabling current autorange during single sweep on {channel}",
                    )

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

            _configure_fast_measurement(active_channel, sweep_current_limit)
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

            def _configure_fast_measurement(channel: str, limit_amps: float | None) -> None:
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
                if limit_amps is not None and limit_amps > 0:
                    self._send_cmd_checked(
                        f"{channel}.measure.autorangei = {channel}.AUTORANGE_OFF",
                        f"disabling current autorange during sweep on {channel}",
                    )
                    self._send_cmd_checked(
                        f"{channel}.measure.rangei = {abs(limit_amps)}",
                        f"setting sweep current range on {channel}",
                    )
                else:
                    self._send_cmd_checked(
                        f"{channel}.measure.autorangei = {channel}.AUTORANGE_ON",
                        f"enabling current autorange during sweep on {channel}",
                    )

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

            _configure_fast_measurement(primary_channel, sweep_current_limit)
            _configure_fast_measurement(secondary_channel, secondary_current_limit)

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
