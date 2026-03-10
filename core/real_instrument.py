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
            return True
        except Exception:
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
    ) -> tuple[list[float], list[float]]:
        """
        Run a linear voltage sweep: evenly spaced points in [start_v, stop_v],
        set voltage and measure current at each point; return (voltages, currents).
        """
        if points < 1:
            return [], []

        step = (stop_v - start_v) / (points - 1) if points > 1 else 0.0
        voltages = [start_v + i * step for i in range(points)]
        currents: list[float] = []

        for v in voltages:
            self._send_cmd(f"{smu_channel}.source.levelv = {v}")
            reply = self._query_cmd(f"print({smu_channel}.measure.i())")
            currents.append(float(reply))

        return voltages, currents
