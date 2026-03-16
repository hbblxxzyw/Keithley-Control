"""
Real instrument driver module.

Implements Keithley 2636/2600B SourceMeter control via PyVISA using TSP commands.
Supports a debug "dry run" mode that requires no physical connection.
Uses the instrument's Trigger Model for high-speed hardware scanning and
chunked data streaming.
"""

import time
import math
import re
from collections.abc import Generator

from core.instrument_base import AbstractSMU
import pyvisa



class RealKeithley2636(AbstractSMU):
    """
    PyVISA-based driver for the real Keithley 2636 instrument.

    Communicates with the instrument using TSP (Test Script Processor) commands.
    When debug=True, no real commands are sent and no physical connection
    is required, for easier testing and development.
    """

    # Connection timeout in milliseconds
    DEFAULT_TIMEOUT_MS = 10000
    DEFAULT_POLL_INTERVAL_S = 0.1
    MIN_CHUNK_POINTS = 5
    MEASURE_EVERY_N_POINTS = 3
    DEFAULT_RESOURCE_ENCODING = "latin-1"

    def __init__(self, debug: bool = False) -> None:
        """
        Args:
            debug: If True, enter dry-run test mode: no real commands sent,
                no physical connection required.
        """
        self.debug = debug
        self._rm = pyvisa.ResourceManager()
        self._resource = None
        self._current_limits: dict[str, float] = {}
        self._source_levels: dict[str, float] = {"smua": 0.0, "smub": 0.0}
        self._source_limits: dict[str, float | None] = {"smua": None, "smub": None}
        self._source_modes: dict[str, str] = {}
        self._measure_state: dict[str, dict[str, object]] = {}

    def _send_cmd(self, cmd: str) -> None:
        """
        Send a single TSP command to the instrument.

        In debug mode only prints the command; otherwise writes via PyVISA.
        """
        if self.debug:
            print(f"[DEBUG SEND] {cmd}")
        else:
            if self._resource is None:
                raise RuntimeError("Instrument not connected; call connect() first.")
            self._resource.write(cmd)

    def _query_cmd(self, cmd: str) -> str:
        """
        Send a TSP command and read the response string.

        In debug mode returns fixed fake data (e.g. '1.23e-6'); otherwise
        uses raw write+read with tolerant decoding.
        """
        if self.debug:
            return "1.23e-6"
        if self._resource is None:
            raise RuntimeError("Instrument not connected; call connect() first.")
        self._resource.write(cmd)
        return self._read_response().strip()

    def _read_response(self) -> str:
        """Read one raw response and decode it robustly."""
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

    def connect(self, resource_str: str) -> bool:
        """
        Connect to the instrument. In debug mode only prints a virtual
        success message; otherwise opens the VISA resource and sets timeout.
        """
        if self.debug:
            print(f"[DEBUG] Virtual connection OK: {resource_str}")
            return True
        try:
            self._resource = self._rm.open_resource(resource_str)
            self._resource.timeout = self.DEFAULT_TIMEOUT_MS
            self._resource.encoding = self.DEFAULT_RESOURCE_ENCODING

            # 设置 TSP 协议必需的指令终止符
            self._resource.read_termination = "\n"
            self._resource.write_termination = "\n"

            # 针对串口连接，强制匹配仪器的波特率
            if "ASRL" in resource_str.upper() or "COM" in resource_str.upper():
                self._resource.baud_rate = 57600

            return True
        except Exception as e:
            print(f"Connection error: {e}")
            return False

    def disconnect(self) -> None:
        """Disconnect and release VISA resources."""
        if self.debug:
            print("[DEBUG] Virtual disconnect")
            return
        if self._resource is not None:
            self._resource.close()
            self._resource = None

    def find_resource_address(self, preferred_serial: str | None = None) -> str | None:
        """Return the most likely VISA resource address for the target instrument."""
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
        """Turn the specified channel (smua/smub) output on or off."""
        on_off = "OUTPUT_ON" if state else "OUTPUT_OFF"
        self._send_cmd(f"{smu_channel}.source.output = {smu_channel}.{on_off}")

    def set_voltage_source(
        self, smu_channel: str, voltage: float, current_limit: float
    ) -> None:
        """Set the specified channel to voltage source mode and compliance."""
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
        """Single current measurement on the given channel; TSP uses print to return value."""
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
        """Configure measurement aperture, current range, and autozero."""
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
        """Measure selected quantities using TSP measure helpers."""
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
            return dict(last_values)  # type: ignore[arg-type]

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
        ramp_up: bool = False,
        ru_step: float = 0.5,
        ru_delay: float = 0.1,
        ramp_down: bool = False,
        rd_step: float = 0.5,
        rd_delay: float = 0.1,
    ) -> Generator[tuple[list[float], list[float]], None, None]:
        """
        Run a linear voltage sweep using the instrument's Trigger Model.

        Ensures output is turned on before the sweep, configures hardware
        trigger (source linearv + measure i), then polls the buffer every 50 ms
        and yields incremental (voltages_chunk, currents_chunk) for low-latency
        streaming. If enabled, ramp-up and ramp-down are executed as separate
        trigger blocks before and after the main sweep.
        """
        if points < 1:
            return

        sweep_current_limit = current_limit
        if sweep_current_limit is None:
            sweep_current_limit = self._current_limits.get(smu_channel)

        def _parse_buffer(reply: str) -> list[float]:
            parts = [p.strip() for p in reply.split(",") if p.strip()]
            out: list[float] = []
            for p in parts:
                try:
                    out.append(float(p))
                except ValueError:
                    continue
            return out

        def _linear_chunk_values(
            v_start: float,
            v_stop: float,
            total_points: int,
            start_point: int,
            end_point: int,
        ) -> list[float]:
            if total_points <= 1:
                return [v_start]
            step_v = (v_stop - v_start) / (total_points - 1)
            return [v_start + (idx - 1) * step_v for idx in range(start_point, end_point + 1)]

        def _configure_fast_measurement(limit_amps: float | None) -> None:
            self._send_cmd(f"{smu_channel}.measure.nplc = {nplc}")
            self._send_cmd(
                f"{smu_channel}.measure.autozero = {smu_channel}.AUTOZERO_OFF"
            )
            self._send_cmd(
                f"{smu_channel}.measure.filter.enable = {smu_channel}.FILTER_OFF"
            )
            if limit_amps is not None and limit_amps > 0:
                self._send_cmd(
                    f"{smu_channel}.measure.autorangei = {smu_channel}.AUTORANGE_OFF"
                )
                self._send_cmd(f"{smu_channel}.measure.rangei = {abs(limit_amps)}")
            else:
                self._send_cmd(
                    f"{smu_channel}.measure.autorangei = {smu_channel}.AUTORANGE_ON"
                )

        # 1) Force the sweep channel to a known 0 V state before enabling output.
        self._send_cmd(f"{smu_channel}.source.func = {smu_channel}.OUTPUT_DCVOLTS")
        if sweep_current_limit is not None:
            self._send_cmd(f"{smu_channel}.source.limiti = {sweep_current_limit}")
        self._send_cmd(f"{smu_channel}.source.levelv = 0")
        # 2) Turn output ON before sweep
        self._send_cmd(f"{smu_channel}.source.output = {smu_channel}.OUTPUT_ON")

        # 3) Buffer: clear, collect source values, append mode; set fast measurement mode
        self._send_cmd(f"{smu_channel}.nvbuffer1.clear()")
        self._send_cmd(f"{smu_channel}.nvbuffer1.collectsourcevalues = 1")
        self._send_cmd(f"{smu_channel}.nvbuffer1.appendmode = 1")
        _configure_fast_measurement(sweep_current_limit)

        # 4) Poll and yield incremental chunks (no fixed sleep)
        old_n = 0

        # 5) Execute trigger blocks for ramp up, main sweep, and ramp down.
        def _execute_trigger_block(v_start: float, v_stop: float, block_pts: int, block_delay: float):
            """Execute a single continuous trigger block (for ramp up, main sweep, and ramp down) and stream data"""
            nonlocal old_n
            if block_pts < 1:
                return

            if self.debug:
                time.sleep(0.1)
                step_v = (v_stop - v_start) / (block_pts - 1) if block_pts > 1 else 0.0
                v_chunk = [v_start + i * step_v for i in range(block_pts)]
                i_chunk = [1.23e-6 + (v - v_start) * 1e-7 for v in v_chunk]
                yield v_chunk, i_chunk
                old_n += block_pts
                return
            # Set specific step delay (e.g. ru_delay, delay, rd_delay)
            self._send_cmd(f"{smu_channel}.source.delay = {block_delay}")
            
            # Configure Trigger Model parameters
            self._send_cmd(f"{smu_channel}.trigger.source.linearv({v_start}, {v_stop}, {block_pts})")
            self._send_cmd(f"{smu_channel}.trigger.source.action = {smu_channel}.ENABLE")
            self._send_cmd(f"{smu_channel}.trigger.measure.i({smu_channel}.nvbuffer1)")
            self._send_cmd(f"{smu_channel}.trigger.measure.action = {smu_channel}.ENABLE")
            self._send_cmd(f"{smu_channel}.trigger.count = {block_pts}")
            self._send_cmd(f"{smu_channel}.trigger.source.stimulus = 0")
            self._send_cmd(f"{smu_channel}.trigger.measure.stimulus = 0")
            
            # Start hardware scan
            self._send_cmd(f"{smu_channel}.trigger.initiate()")

            # poll and pull incremental data for this block
            block_base_n = old_n
            target_n = old_n + block_pts
            while old_n < target_n:
                time.sleep(self.DEFAULT_POLL_INTERVAL_S)
                current_n = int(
                    float(self._query_cmd(f"print({smu_channel}.nvbuffer1.n)"))
                )
                if current_n <= old_n:
                    continue
                if (
                    current_n < target_n
                    and current_n - old_n < self.MIN_CHUNK_POINTS
                ):
                    continue
                # Pull only new data: indices old_n+1 .. current_n (1-based in TSP)
                reply_i = self._query_cmd(
                    f"printbuffer({old_n + 1}, {current_n}, "
                    f"{smu_channel}.nvbuffer1.readings)"
                )
                i_chunk = _parse_buffer(reply_i)
                start_point = old_n - block_base_n + 1
                end_point = current_n - block_base_n
                v_chunk = _linear_chunk_values(
                    v_start,
                    v_stop,
                    block_pts,
                    start_point,
                    end_point,
                )
                n_chunk = min(len(v_chunk), len(i_chunk))
                if n_chunk > 0:
                    yield v_chunk[:n_chunk], i_chunk[:n_chunk]
                old_n = current_n

        # ================= Three steps sequentially execution =================

        # Step A：Ramp Up
        if ramp_up and abs(start_v) > 0:
            # Calculate safe points based on ru_step (round up to ensure actual step <= ru_step)
            ru_pts = max(2, int(math.ceil(abs(start_v) / max(ru_step, 1e-9))) + 1)
            yield from _execute_trigger_block(0.0, start_v, ru_pts, ru_delay)

        # Step B：Main Sweep
        yield from _execute_trigger_block(start_v, stop_v, points, delay)

        # Step C：Ramp Down
        if ramp_down and abs(stop_v) > 0:
            rd_pts = max(2, int(math.ceil(abs(stop_v) / max(rd_step, 1e-9))) + 1)
            yield from _execute_trigger_block(stop_v, 0.0, rd_pts, rd_delay)
