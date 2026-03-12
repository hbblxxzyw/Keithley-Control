"""
Real instrument driver module.

Implements Keithley 2636/2600B SourceMeter control via PyVISA using TSP commands.
Supports a debug "dry run" mode that requires no physical connection.
Uses the instrument's Trigger Model for high-speed hardware scanning and
chunked data streaming.
"""

import time
import math
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

    def __init__(self, debug: bool = False) -> None:
        """
        Args:
            debug: If True, enter dry-run test mode: no real commands sent,
                no physical connection required.
        """
        self.debug = debug
        self._rm = pyvisa.ResourceManager()
        self._resource = None

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
        uses PyVISA query for write+read.
        """
        if self.debug:
            return "1.23e-6"
        if self._resource is None:
            raise RuntimeError("Instrument not connected; call connect() first.")
        return self._resource.query(cmd).strip()

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

    def set_output(self, smu_channel: str, state: bool) -> None:
        """Turn the specified channel (smua/smub) output on or off."""
        on_off = "OUTPUT_ON" if state else "OUTPUT_OFF"
        self._send_cmd(f"{smu_channel}.source.output = {smu_channel}.{on_off}")

    def set_voltage_source(
        self, smu_channel: str, voltage: float, current_limit: float
    ) -> None:
        """Set the specified channel to voltage source mode and compliance."""
        self._send_cmd(f"{smu_channel}.source.func = {smu_channel}.OUTPUT_DCVOLTS")
        self._send_cmd(f"{smu_channel}.source.levelv = {voltage}")
        self._send_cmd(f"{smu_channel}.source.limiti = {current_limit}")

    def measure_current(self, smu_channel: str) -> float:
        """Single current measurement on the given channel; TSP uses print to return value."""
        reply = self._query_cmd(f"print({smu_channel}.measure.i())")
        return float(reply)

    def run_iv_sweep(
        self,
        smu_channel: str,
        start_v: float,
        stop_v: float,
        points: int,
        delay: float = 0.0,
        nplc: float = 1.0,
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
        streaming. ramp_up/ramp_down are accepted for API compatibility but
        are not used in the trigger-model path.
        """
        if points < 1:
            return

        def _parse_buffer(reply: str) -> list[float]:
            parts = [p.strip() for p in reply.split(",") if p.strip()]
            out: list[float] = []
            for p in parts:
                try:
                    out.append(float(p))
                except ValueError:
                    continue
            return out

        # 1) Turn output ON before sweep 
        self._send_cmd(f"{smu_channel}.source.output = {smu_channel}.OUTPUT_ON")

        # 2) Buffer: clear, collect source values, append mode; set NPLC
        self._send_cmd(f"{smu_channel}.nvbuffer1.clear()")
        self._send_cmd(f"{smu_channel}.nvbuffer1.collectsourcevalues = 1")
        self._send_cmd(f"{smu_channel}.nvbuffer1.appendmode = 1")
        self._send_cmd(f"{smu_channel}.measure.nplc = {nplc}")

        # 3) Poll and yield incremental chunks (no fixed sleep)
        old_n = 0

        # 4) Execute trogger block for supporting ramp up and down
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
            target_n = old_n + block_pts
            import time
            while old_n < target_n:
                time.sleep(0.05)
                current_n = int(
                    float(self._query_cmd(f"print({smu_channel}.nvbuffer1.n)"))
                )
                if current_n <= old_n:
                    continue
                # Pull only new data: indices old_n+1 .. current_n (1-based in TSP)
                reply_v = self._query_cmd(
                    f"printbuffer({old_n + 1}, {current_n}, "
                    f"{smu_channel}.nvbuffer1.sourcevalues)"
                )
                reply_i = self._query_cmd(
                    f"printbuffer({old_n + 1}, {current_n}, "
                    f"{smu_channel}.nvbuffer1.readings)"
                )
                v_chunk = _parse_buffer(reply_v)
                i_chunk = _parse_buffer(reply_i)
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