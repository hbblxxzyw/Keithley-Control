"""
Dummy instrument driver for testing the GUI without hardware.

Implements AbstractSMU with no physical I/O; connect always succeeds,
measurements return fake data.
"""

from collections.abc import Callable, Generator

from core.instrument_base import AbstractSMU


class DummyKeithley2636(AbstractSMU):
    """
    Dummy Keithley 2636: same interface as RealKeithley2636 but no hardware.
    Use for safe GUI/testing; switch to RealKeithley2636 when ready.
    """

    def __init__(self) -> None:
        self._source_levels: dict[str, float] = {"smua": 0.0, "smub": 0.0}

    def connect(self, resource_str: str) -> bool:
        """Accept any address and report success."""
        return True

    def disconnect(self) -> None:
        """No-op."""
        pass

    def get_model(self) -> str:
        """Return the simulated instrument model."""
        return "2636B"

    def set_output(self, smu_channel: str, state: bool) -> None:
        """No-op."""
        pass

    def set_voltage_source(
        self, smu_channel: str, voltage: float, current_limit: float
    ) -> None:
        """No-op."""
        self._source_levels[smu_channel] = float(voltage)

    def measure_current(self, smu_channel: str) -> float:
        """Return a fixed fake current (A)."""
        return 1.23e-6

    def configure_measurement(
        self,
        smu_channel: str,
        measurement_items: list[str],
        current_range: str,
        autozero: str,
        nplc: float,
    ) -> None:
        """No-op."""
        pass

    def measure_selected(
        self, smu_channel: str, measurement_items: list[str]
    ) -> dict[str, float]:
        """Return deterministic fake readings for the selected quantities."""
        voltage = float(self._source_levels.get(smu_channel, 0.0))
        current = 1.23e-6 + voltage * 1e-7
        resistance = voltage / current if abs(current) > 1e-15 else float("inf")
        values = {
            "Voltage": voltage,
            "Current": current,
            "Resistance": resistance,
        }
        return {item: values[item] for item in measurement_items if item in values}

    def dump_errors(self) -> list[str]:
        """Dummy driver has no instrument-side error queue."""
        return []

    def abort_sweep(self) -> None:
        """Dummy driver has no active instrument sweep to abort."""
        return None

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
        """Yield one chunk of linear voltage sweep and fake currents."""
        if points < 1:
            return
        if stop_checker is not None and stop_checker():
            raise InterruptedError("Sweep aborted by user.")
        secondary_channel = "smub" if smu_channel == "smua" else "smua"
        step = (stop_v - start_v) / (points - 1) if points > 1 else 0.0
        voltages = [start_v + i * step for i in range(points)]
        currents = [1.23e-6 + (v - start_v) * 1e-7 for v in voltages]
        if str(secondary_mode).strip().lower() == "linear":
            sec_start = secondary_start_v if secondary_start_v is not None else secondary_level
            sec_stop = secondary_stop_v if secondary_stop_v is not None else secondary_level
            sec_step = (sec_stop - sec_start) / (points - 1) if points > 1 else 0.0
            secondary_source_values = [sec_start + i * sec_step for i in range(points)]
        else:
            secondary_source_values = [float(secondary_level)] * points
        secondary_voltage = float(
            secondary_source_values[-1]
            if secondary_source_values
            else self._source_levels.get(secondary_channel, 0.0)
        )
        secondary_currents = [
            8.9e-7 + secondary_source * 8e-8 + index * 1e-9
            for index, secondary_source in enumerate(secondary_source_values)
        ]
        measured_voltages = None
        secondary_measured_voltages = None
        timestamps = [float(index) * max(delay, 0.0) for index in range(points)]
        if measurement_items and any(
            item in {"Voltage", "Resistance"} for item in measurement_items
        ):
            measured_voltages = list(voltages)
        yield (
            voltages,
            currents,
            measured_voltages,
            secondary_source_values,
            secondary_currents,
            secondary_measured_voltages,
            timestamps,
        )
        self._source_levels[smu_channel] = float(voltages[-1])
        self._source_levels[secondary_channel] = float(secondary_voltage)
