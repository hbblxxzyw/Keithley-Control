"""
Dummy instrument driver for testing the GUI without hardware.

Implements AbstractSMU with no physical I/O; connect always succeeds,
measurements return fake data.
"""

from collections.abc import Generator

from core.instrument_base import AbstractSMU


class DummyKeithley2636(AbstractSMU):
    """
    Dummy Keithley 2636: same interface as RealKeithley2636 but no hardware.
    Use for safe GUI/testing; switch to RealKeithley2636 when ready.
    """

    def connect(self, resource_str: str) -> bool:
        """Accept any address and report success."""
        return True

    def disconnect(self) -> None:
        """No-op."""
        pass

    def set_output(self, smu_channel: str, state: bool) -> None:
        """No-op."""
        pass

    def set_voltage_source(
        self, smu_channel: str, voltage: float, current_limit: float
    ) -> None:
        """No-op."""
        pass

    def measure_current(self, smu_channel: str) -> float:
        """Return a fixed fake current (A)."""
        return 1.23e-6

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
        """Yield one chunk of linear voltage sweep and fake currents."""
        if points < 1:
            return
        step = (stop_v - start_v) / (points - 1) if points > 1 else 0.0
        voltages = [start_v + i * step for i in range(points)]
        currents = [1.23e-6 + (v - start_v) * 1e-7 for v in voltages]
        yield voltages, currents
