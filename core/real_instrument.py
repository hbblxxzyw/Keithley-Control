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
from collections.abc import Generator

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
    MIN_CHUNK_POINTS = 2
    MAX_POINTS_PER_PRINTBUFFER = 8
    MEASURE_EVERY_N_POINTS = 3
    DEFAULT_RESOURCE_ENCODING = "latin-1"

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

    def _send_multiline(self, script_text: str) -> None:
        self._send_cmd(script_text.strip())

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
        count = int(float(self._query_cmd("print(errorqueue.count)")))
        errors: list[str] = []
        for _ in range(max(count, 0)):
            errors.append(self._query_cmd("print(errorqueue.next())"))
        return errors

    def _ensure_dual_sync_script_loaded(self) -> None:
        if self._dual_sync_script_loaded:
            return
        if self.debug:
            self._dual_sync_script_loaded = True
            return

        self._send_multiline(
            """
oai_dualsync = oai_dualsync or {}

function oai_dualsync._prepare_buffers(smu)
    smu.nvbuffer1.clear()
    smu.nvbuffer1.clearcache()
    smu.nvbuffer1.appendmode = 1
    smu.nvbuffer1.collectsourcevalues = 1
    smu.nvbuffer1.collecttimestamps = 1

    smu.nvbuffer2.clear()
    smu.nvbuffer2.clearcache()
    smu.nvbuffer2.appendmode = 1
    smu.nvbuffer2.collectsourcevalues = 1
    smu.nvbuffer2.collecttimestamps = 1
end

function oai_dualsync._build_linear_list(start_v, stop_v, points)
    local values = {}
    local step_v = 0
    if points > 1 then
        step_v = (stop_v - start_v) / (points - 1)
    end
    for index = 1, points do
        values[index] = start_v + (index - 1) * step_v
    end
    return values
end

function oai_dualsync._build_constant_list(level_v, points)
    local values = {}
    for index = 1, points do
        values[index] = level_v
    end
    return values
end

function oai_dualsync.configure_block(
    primary,
    secondary,
    primary_start,
    primary_stop,
    points,
    step_delay,
    primary_limit,
    secondary_limit,
    secondary_mode,
    secondary_level,
    secondary_start,
    secondary_stop,
    capture_primary_voltage,
    capture_secondary_voltage
)
    local secondary_values

    if secondary_mode == 1 then
        secondary_values = oai_dualsync._build_linear_list(
            secondary_start,
            secondary_stop,
            points
        )
    else
        secondary_values = oai_dualsync._build_constant_list(secondary_level, points)
    end

    primary.abort()
    secondary.abort()
    primary.trigger.clear()
    secondary.trigger.clear()
    trigger.blender[1].reset()

    oai_dualsync._prepare_buffers(primary)
    oai_dualsync._prepare_buffers(secondary)

    primary.source.func = primary.OUTPUT_DCVOLTS
    secondary.source.func = secondary.OUTPUT_DCVOLTS

    if primary_limit ~= nil and primary_limit > 0 then
        primary.source.limiti = primary_limit
    end
    if secondary_limit ~= nil and secondary_limit > 0 then
        secondary.source.limiti = secondary_limit
    end

    primary.source.levelv = primary_start
    secondary.source.levelv = secondary_values[1]
    primary.source.delay = step_delay
    secondary.source.delay = step_delay

    primary.trigger.source.linearv(primary_start, primary_stop, points)
    secondary.trigger.source.listv(secondary_values)
    primary.trigger.source.action = primary.ENABLE
    secondary.trigger.source.action = secondary.ENABLE

    if capture_primary_voltage ~= 0 then
        primary.trigger.measure.iv(primary.nvbuffer1, primary.nvbuffer2)
    else
        primary.trigger.measure.i(primary.nvbuffer1)
    end
    if capture_secondary_voltage ~= 0 then
        secondary.trigger.measure.iv(secondary.nvbuffer1, secondary.nvbuffer2)
    else
        secondary.trigger.measure.i(secondary.nvbuffer1)
    end

    primary.trigger.measure.action = primary.ENABLE
    secondary.trigger.measure.action = secondary.ENABLE
    primary.trigger.count = points
    secondary.trigger.count = points
    primary.trigger.arm.count = 1
    secondary.trigger.arm.count = 1

    primary.trigger.arm.stimulus = 0
    secondary.trigger.arm.stimulus = primary.trigger.ARMED_EVENT_ID

    primary.trigger.source.stimulus = 0
    secondary.trigger.source.stimulus = 0

    trigger.blender[1].orenable = false
    trigger.blender[1].stimulus[1] = primary.trigger.SOURCE_COMPLETE_EVENT_ID
    trigger.blender[1].stimulus[2] = secondary.trigger.SOURCE_COMPLETE_EVENT_ID
    primary.trigger.measure.stimulus = trigger.blender[1].EVENT_ID
    secondary.trigger.measure.stimulus = trigger.blender[1].EVENT_ID

    primary.trigger.endsweep.action = primary.SOURCE_HOLD
    secondary.trigger.endsweep.action = secondary.SOURCE_HOLD

    primary.source.output = primary.OUTPUT_ON
    secondary.source.output = secondary.OUTPUT_ON

    secondary.trigger.initiate()
    primary.trigger.initiate()
end

function oai_dualsync.wait_done()
    waitcomplete()
end
            """
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

    def _format_tsp_value(self, value: float | int | None) -> str:
        if value is None:
            return "nil"
        if isinstance(value, bool):
            return "1" if value else "0"
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
        self._send_cmd(f"{smu_channel}.source.output = {smu_channel}.{on_off}")

    def set_voltage_source(
        self, smu_channel: str, voltage: float, current_limit: float
    ) -> None:
        if self._source_modes.get(smu_channel) != "voltage":
            self._send_cmd(f"{smu_channel}.source.func = {smu_channel}.OUTPUT_DCVOLTS")
            self._source_modes[smu_channel] = "voltage"

        last_limit = self._source_limits.get(smu_channel)
        if last_limit is None or abs(last_limit - float(current_limit)) > 1e-15:
            self._send_cmd(f"{smu_channel}.source.limiti = {current_limit}")
            self._source_limits[smu_channel] = float(current_limit)

        last_level = self._source_levels.get(smu_channel)
        if last_level is None or abs(last_level - float(voltage)) > 1e-15:
            self._send_cmd(f"{smu_channel}.source.levelv = {voltage}")
        self._source_levels[smu_channel] = float(voltage)
        if current_limit > 0:
            self._current_limits[smu_channel] = float(current_limit)

    def measure_current(self, smu_channel: str) -> float:
        reply = self._query_cmd(f"print({smu_channel}.measure.i())")
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

        self._send_cmd(f"{smu_channel}.measure.nplc = {float(nplc)}")
        self._send_cmd(
            f"{smu_channel}.measure.autozero = {smu_channel}.{autozero_token}"
        )
        self._send_cmd(
            f"{smu_channel}.measure.filter.enable = {smu_channel}.FILTER_OFF"
        )

        range_amps = self._measurement_range_amps(current_range)
        if range_amps is None:
            self._send_cmd(
                f"{smu_channel}.measure.autorangei = {smu_channel}.AUTORANGE_ON"
            )
        else:
            self._send_cmd(
                f"{smu_channel}.measure.autorangei = {smu_channel}.AUTORANGE_OFF"
            )
            self._send_cmd(f"{smu_channel}.measure.rangei = {range_amps}")

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
        reply = self._query_cmd(f"print({expr})")
        parts = [part for part in re.split(r"[\t,\r\n ]+", reply.strip()) if part]

        out: dict[str, float] = {}
        for (item, _), value in zip(selected, parts):
            out[item] = float(value)
        state["last"] = dict(out)
        return out

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

            def _configure_fast_measurement(channel: str, limit_amps: float | None) -> None:
                self._send_cmd(f"{channel}.measure.nplc = {float(nplc)}")
                self._send_cmd(f"{channel}.measure.filter.enable = {channel}.FILTER_OFF")
                self._send_cmd(f"{channel}.measure.autozero = {channel}.AUTOZERO_ONCE")
                self._send_cmd(f"{channel}.measure.autozero = {channel}.AUTOZERO_OFF")
                if limit_amps is not None and limit_amps > 0:
                    self._send_cmd(f"{channel}.measure.autorangei = {channel}.AUTORANGE_OFF")
                    self._send_cmd(f"{channel}.measure.rangei = {abs(limit_amps)}")
                else:
                    self._send_cmd(f"{channel}.measure.autorangei = {channel}.AUTORANGE_ON")

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
            ]:
                # FIX: Only fetch essential readings during realtime streaming to keep the output queue small.
                columns = [f"{primary_channel}.nvbuffer1.readings"]
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

                reply = self._query_cmd(
                    f"printbuffer({start_index}, {end_index}, {', '.join(columns)})"
                )
                rows = self._reshape_printbuffer_rows(reply, len(columns))

                primary_currents = [row[0] for row in rows]
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
                )

            def _start_block(
                block_start: float,
                block_stop: float,
                block_points: int,
                block_delay: float,
            ) -> None:
                self._send_cmd(
                    "oai_dualsync.configure_block("
                    f"{primary_channel}, "
                    f"{secondary_channel}, "
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
                    f"{1 if capture_secondary_voltage else 0})"
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
                    current_n = int(float(self._query_cmd(f"print({primary_channel}.nvbuffer1.n)")))
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
                                [],  # FIX: Do not stream timestamps in realtime; keep payload minimal.
                            )

                        pull_start = pull_end + 1

                    block_old_n = current_n

                self._query_cmd("oai_dualsync.wait_done() print(1)")
                self._source_levels[primary_channel] = float(block_stop)
                self._source_levels[secondary_channel] = float(
                    secondary_stop if secondary_mode_token == 1 else secondary_level
                )

            _configure_fast_measurement(primary_channel, sweep_current_limit)
            _configure_fast_measurement(secondary_channel, secondary_current_limit)

            if ramp_up and abs(start_v) > 0:
                ramp_up_points = max(2, int(math.ceil(abs(start_v) / max(ru_step, 1e-9))) + 1)
                yield from _run_block(0.0, start_v, ramp_up_points, ru_delay)

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
