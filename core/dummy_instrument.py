"""
Dummy instrument driver for testing the GUI without hardware.

Implements AbstractSMU with no physical I/O; connect always succeeds,
measurements return fake data.
"""

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
    ) -> tuple[list[float], list[float]]:
        """Return linear voltage sweep and fake currents."""
        if points < 1:
            return [], []
        step = (stop_v - start_v) / (points - 1) if points > 1 else 0.0
        voltages = [start_v + i * step for i in range(points)]
        # Fake current: small linear ramp for a plausible-looking curve
        currents = [1.23e-6 + (v - start_v) * 1e-7 for v in voltages]
        return voltages, currents
