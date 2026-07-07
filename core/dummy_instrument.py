"""
Dummy instrument driver for testing the GUI without hardware.

Implements AbstractSMU with no physical I/O; connect always succeeds,
measurements return fake data.
"""

from collections.abc import Callable, Generator

from core.instrument_base import AbstractSMU
from core.pulse_sequence import PulseEvent, PulseTimelinePoint


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

    def is_connected(self, timeout_ms: int = 300) -> bool:
        """Report the simulator as always reachable."""
        return True

    def get_model(self) -> str:
        """Return the simulated instrument model."""
        return "2636B Simulator"

    def find_resource_address(self, preferred_serial: str | None = None) -> str:
        """Return a fake resource address so GUI auto-connect works offline."""
        return "DUMMY::KEITHLEY2636::INSTR"

    def set_output(self, smu_channel: str, state: bool) -> None:
        """No-op."""
        pass

    def set_voltage_source(
        self, smu_channel: str, voltage: float, current_limit: float
    ) -> None:
        """No-op."""
        self._source_levels[smu_channel] = float(voltage)

    def set_current_source(
        self, smu_channel: str, current: float, voltage_limit: float
    ) -> None:
        """No-op."""
        self._source_levels[smu_channel] = float(current)

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
        """Yield deterministic fake pulse readings for offline GUI testing."""
        if not events:
            return
        if stop_checker is not None and stop_checker():
            raise InterruptedError("Pulse run aborted by user.")

        normalized_mode = str(source_mode or "voltage").strip().lower()
        levels = [float(event.level) for event in events]
        timestamps: list[float] = []
        elapsed_s = 0.0
        for event in events:
            timestamps.append(elapsed_s)
            elapsed_s += max(float(event.width_s), 0.0) + max(
                float(event.interval_after_s), 0.0
            )

        if normalized_mode == "current":
            currents = list(levels)
            voltages = [
                0.05 + level * 1.0e5 + index * 1e-4
                for index, level in enumerate(levels)
            ]
            self._source_levels[smu_channel] = 0.0
        else:
            voltages = list(levels)
            currents = [
                1.23e-6 + level * 1e-7 + index * 1e-9
                for index, level in enumerate(levels)
            ]
            self._source_levels[smu_channel] = 0.0

        yield (levels, currents, voltages, timestamps)

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
        """Yield deterministic fake readings for every pulse timeline point."""
        if not timeline:
            return
        if stop_checker is not None and stop_checker():
            raise InterruptedError("Pulse run aborted by user.")

        normalized_mode = str(source_mode or "voltage").strip().lower()
        levels = [float(point.source_level) for point in timeline]
        timestamps = [float(point.time_s) for point in timeline]
        if normalized_mode == "current":
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

        bias_sources: list[float] | None = None
        bias_currents: list[float] | None = None
        bias_voltages: list[float] | None = None
        if isinstance(bias_config, dict):
            bias_level = float(bias_config.get("level", 0.0))
            bias_mode = str(bias_config.get("source_mode", "voltage")).strip().lower()
            bias_sources = [bias_level] * len(levels)
            if bias_mode == "current":
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
            channel = str(bias_config.get("channel", "")).strip()
            if channel:
                self._source_levels[channel] = bias_level

        self._source_levels[smu_channel] = 0.0
        yield (
            levels,
            currents,
            voltages,
            bias_sources,
            bias_currents,
            bias_voltages,
            timestamps,
        )

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
        """Yield deterministic fake readings for a pulse source-list sweep."""
        if callable(bias_config) and stop_checker is None:
            stop_checker = bias_config
            bias_config = measurement_items if isinstance(measurement_items, dict) else None
            measurement_items = (
                list(source_limit)
                if isinstance(source_limit, list)
                else None
            )
            source_limit = float(nplc) if isinstance(nplc, (int, float)) else None
            nplc = 1.0

        if not source_levels:
            return
        if stop_checker is not None and stop_checker():
            raise InterruptedError("Pulse run aborted by user.")

        normalized_mode = str(source_mode or "voltage").strip().lower()
        levels = [float(level) for level in source_levels]
        linefreq_hz = 50.0
        point_interval_s = max(float(nplc), 0.0) / linefreq_hz + max(
            float(src_to_meas_delay_s),
            0.0,
        )
        timestamps = [
            float(index) * point_interval_s for index in range(len(levels))
        ]

        if normalized_mode == "current":
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

        bias_sources: list[float] | None = None
        bias_currents: list[float] | None = None
        bias_voltages: list[float] | None = None
        if isinstance(bias_config, dict):
            bias_level = float(bias_config.get("level", 0.0))
            bias_mode = str(bias_config.get("source_mode", "voltage")).strip().lower()
            bias_sources = [bias_level] * len(levels)
            if bias_mode == "current":
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
            channel = str(bias_config.get("channel", "")).strip()
            if channel:
                self._source_levels[channel] = bias_level

        self._source_levels[smu_channel] = 0.0
        yield (
            levels,
            currents,
            voltages,
            bias_sources,
            bias_currents,
            bias_voltages,
            timestamps,
        )

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
        """Yield one chunk of a single-channel sweep and fake currents."""
        if points < 1:
            return
        if stop_checker is not None and stop_checker():
            raise InterruptedError("Sweep aborted by user.")
        inactive_channel = "smub" if smu_channel == "smua" else "smua"
        self._source_levels[inactive_channel] = 0.0
        step = (stop_v - start_v) / (points - 1) if points > 1 else 0.0
        voltages = [start_v + i * step for i in range(points)]
        currents = [1.23e-6 + (v - start_v) * 1e-7 for v in voltages]
        measured_voltages = None
        timestamps = [float(index) * max(delay, 0.0) for index in range(points)]
        if measurement_items and any(
            item in {"Voltage", "Resistance"} for item in measurement_items
        ):
            measured_voltages = list(voltages)
        yield (voltages, currents, measured_voltages, timestamps)
        self._source_levels[smu_channel] = float(voltages[-1])
