"""
Real instrument driver module.

Implements Keithley 2636 SourceMeter control via PyVISA using TSP commands.
Supports a debug "dry run" mode that requires no physical connection.
"""

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
        self._send_cmd(f"{smu_channel}.source.ilimit = {current_limit}")

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
    ) -> tuple[list[float], list[float]]:
        """
        Run a linear voltage sweep by pushing a TSP script to the instrument.

        The script performs the sweep on the instrument side (optionally with
        safe_ramp up/down), stores voltages and currents in nvbuffer1, then
        we fetch the data dynamically by buffer length.
        """
        if points < 1:
            return [], []

        # Configure buffer: clear, enable source value collection, append mode
        self._send_cmd(f"{smu_channel}.nvbuffer1.clear()")
        self._send_cmd(f"{smu_channel}.nvbuffer1.collectsourcevalues = 1")
        self._send_cmd(f"{smu_channel}.nvbuffer1.appendmode = 1")
        self._send_cmd(f"{smu_channel}.measure.nplc = {nplc}")

        # Lua safe_ramp: ramps voltage with steps, measures at each step into buffer
        safe_ramp_def = """
function safe_ramp(channel, target_v, step_v, delay_s, buffer)
    local current_v = channel.source.levelv
    if current_v == nil then current_v = 0.0 end
    local diff = target_v - current_v
    local dir = 1
    if diff < 0 then dir = -1 end
    while math.abs(target_v - current_v) > step_v do
        current_v = current_v + dir * step_v
        channel.source.levelv = current_v
        delay(delay_s)
        channel.measure.i(buffer)
    end
    channel.source.levelv = target_v
    delay(delay_s)
    channel.measure.i(buffer)
end
"""
        # Build main script: define safe_ramp, then optional ramp_up, sweep loop, optional ramp_down
        script = safe_ramp_def + f"""
local start_v = {start_v}
local stop_v = {stop_v}
local points = {points}
local delay_s = {delay}
local step_v
if points <= 1 then
    step_v = 0
else
    step_v = (stop_v - start_v) / (points - 1)
end
"""
        if ramp_up:
            script += f"""
{smu_channel}.source.levelv = 0
{smu_channel}.source.output = {smu_channel}.OUTPUT_ON
safe_ramp({smu_channel}, start_v, {ru_step}, {ru_delay}, {smu_channel}.nvbuffer1)
"""
        script += f"""
for i = 0, points - 1 do
    local v = start_v + i * step_v
    {smu_channel}.source.levelv = v
    delay(delay_s)
    {smu_channel}.measure.i({smu_channel}.nvbuffer1)
end
"""
        if ramp_down:
            script += f"""
safe_ramp({smu_channel}, 0.0, {rd_step}, {rd_delay}, {smu_channel}.nvbuffer1)
"""
        # 以匿名脚本模式加载多行 TSP 代码，避免 -285 语法错误
        self._send_cmd("loadscript")
        for line in script.strip().split("\n"):
            if line.strip():  # 跳过空行
                self._send_cmd(line.strip())
        self._send_cmd("endscript")

        # 指挥仪器立刻运行刚刚加载的脚本
        self._send_cmd("script.anonymous.run()")

        # Blocking wait: sweep + optional ramp time
        import time as _time

        est_step_time = max(delay, nplc / 50.0)
        total_wait = max(0.1, est_step_time * points)
        if ramp_up:
            total_wait += max(0.1, abs(start_v) / max(ru_step, 1e-9) * ru_delay + 1)
        if ramp_down:
            total_wait += max(0.1, abs(stop_v) / max(rd_step, 1e-9) * rd_delay + 1)
        _time.sleep(total_wait)

        # Dynamic read: get buffer size then read sourcevalues and readings
        n_pts = int(self._query_cmd(f"print({smu_channel}.nvbuffer1.n)"))
        if n_pts < 1:
            return [], []

        reply_v = self._query_cmd(
            f"printbuffer(1, {n_pts}, {smu_channel}.nvbuffer1.sourcevalues)"
        )
        reply_i = self._query_cmd(
            f"printbuffer(1, {n_pts}, {smu_channel}.nvbuffer1.readings)"
        )

        def _parse_buffer(reply: str) -> list[float]:
            parts = [p.strip() for p in reply.split(",") if p.strip()]
            out: list[float] = []
            for p in parts:
                try:
                    out.append(float(p))
                except ValueError:
                    continue
            return out

        voltages = _parse_buffer(reply_v)
        currents = _parse_buffer(reply_i)
        # Trim to same length (instrument may return slightly different counts)
        n = min(len(voltages), len(currents), n_pts)
        return voltages[:n], currents[:n]
