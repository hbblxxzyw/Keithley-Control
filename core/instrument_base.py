"""
Instrument abstract base class module.

Provides the AbstractSMU abstract base class as a unified interface template
for all real and virtual instruments.
"""

from abc import ABC, abstractmethod
from collections.abc import Callable
from typing import Generator


class AbstractSMU(ABC):
    """
    Source Measure Unit (SMU) abstract base class.

    Defines the minimal interface required for interacting with SMU instruments
    such as the Keithley 2636. Both real instrument drivers and virtual
    instruments (simulation/offline) must inherit and implement this class.
    """

    @abstractmethod
    def connect(self, resource_str: str) -> bool:
        """
        Connect to the instrument.

        Args:
            resource_str: Instrument resource string, e.g. VISA address
                ('TCPIP0::192.168.1.1::inst0::INSTR') or serial ('ASRL3::INSTR').

        Returns:
            True if connection succeeded, False if it failed.
        """
        pass

    @abstractmethod
    def disconnect(self) -> None:
        """
        Disconnect from the instrument and release resources.
        """
        pass

    @abstractmethod
    def set_output(self, smu_channel: str, state: bool) -> None:
        """
        Turn the specified channel output on or off.

        Args:
            smu_channel: Channel identifier, either 'smua' or 'smub'.
            state: True to enable output, False to disable.
        """
        pass

    @abstractmethod
    def set_voltage_source(
        self, smu_channel: str, voltage: float, current_limit: float
    ) -> None:
        """
        Set the specified channel to constant voltage source mode and set
        voltage and current compliance limit.

        Args:
            smu_channel: Channel identifier, either 'smua' or 'smub'.
            voltage: Source voltage in V.
            current_limit: Current compliance limit in A; exceeding this
                enters compliance state.
        """
        pass

    @abstractmethod
    def set_current_source(
        self, smu_channel: str, current: float, voltage_limit: float
    ) -> None:
        """
        Set the specified channel to constant current source mode and set
        current and voltage compliance limit.
        """
        pass

    @abstractmethod
    def measure_current(self, smu_channel: str) -> float:
        """
        Perform a single current measurement on the specified channel.

        Args:
            smu_channel: Channel identifier, either 'smua' or 'smub'.

        Returns:
            Measured current in A.
        """
        pass

    @abstractmethod
    def configure_measurement(
        self,
        smu_channel: str,
        measurement_items: list[str],
        current_range: str,
        autozero: str,
        nplc: float,
    ) -> None:
        """
        Configure measurement parameters for the specified channel.

        Args:
            smu_channel: Channel identifier, either 'smua' or 'smub'.
            measurement_items: Selected measurement quantities.
            current_range: Current range label such as 'Auto' or '1 mA'.
            autozero: Autozero mode label such as 'On', 'Off', or 'Once'.
            nplc: Integration time in PLC.
        """
        pass

    @abstractmethod
    def measure_selected(
        self, smu_channel: str, measurement_items: list[str]
    ) -> dict[str, float]:
        """
        Measure the selected quantities on the specified channel.

        Args:
            smu_channel: Channel identifier, either 'smua' or 'smub'.
            measurement_items: Selected measurement quantities.

        Returns:
            A mapping from quantity name to measured value.
        """
        pass

    def dump_errors(self) -> list[str]:
        """
        Return instrument error queue entries, if supported.

        Drivers without an instrument-side error queue may return an empty
        list.
        """
        return []

    def abort_sweep(self) -> None:
        """
        Abort the active sweep, if supported.

        Drivers that do not maintain instrument-side sweep state may no-op.
        """
        return None

    @abstractmethod
    def run_pulse_sequence(
        self,
        smu_channel: str,
        source_mode: str,
        events: list[object],
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
        """
        Run a single-SMU pulse sequence using the instrument trigger model.

        Yields chunks of (
            source_levels,
            current_values,
            voltage_values,
            timestamps,
        ). Implementations may use source-only/minimal measurements, but
        should return enough data for the GUI table when practical.
        """
        pass

    @abstractmethod
    def run_pulse_timeline(
        self,
        smu_channel: str,
        source_mode: str,
        timeline: list[object],
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
        """
        Run a full time-resolved pulse list sweep.

        Yields chunks of (
            active_source_levels,
            active_current_values,
            active_voltage_values,
            bias_source_levels,
            bias_current_values,
            bias_voltage_values,
            timestamps,
        ).
        """
        pass

    @abstractmethod
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
        """
        Run a linear voltage sweep on one enabled SMU only.

        Implementations should explicitly turn the other SMU output off and
        keep it out of the trigger model for the duration of this sweep.
        """
        pass

    @abstractmethod
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
        """
        Run a linear voltage sweep (IV sweep) on the specified channel.

        Yields chunks of (voltages, currents) for low-latency streaming;
        callers can plot or process data incrementally.

        Args:
            smu_channel: Channel identifier, either 'smua' or 'smub'.
            start_v: Start voltage in V.
            stop_v: Stop voltage in V.
            points: Number of sweep points (including endpoints); voltage
                is linearly spaced between start_v and stop_v.
            delay: Source-to-measure delay in seconds for each step.
            nplc: Integration time in PLC (power-line cycles) per measurement.
            current_limit: Optional source current compliance limit in A.
            measurement_items: Requested quantities for the primary sweep
                channel. If voltage or resistance is requested, implementations
                may return measured voltages in addition to currents.
            ramp_up: If True, ramp from 0 V to start_v before the sweep.
            ru_step: Ramp-up voltage step in V.
            ru_delay: Ramp-up step delay in s.
            ramp_down: If True, ramp from stop_v to 0 V after the sweep.
            rd_step: Ramp-down voltage step in V.
            rd_delay: Ramp-down step delay in s.
            secondary_mode: Secondary channel sweep mode for the block.
                "fixed" produces a constant source list; "linear" produces a
                linearly spaced synchronized sweep.
            secondary_level: Secondary constant-bias level in V when
                secondary_mode == "fixed".
            secondary_start_v: Optional secondary sweep start in V when
                secondary_mode == "linear".
            secondary_stop_v: Optional secondary sweep stop in V when
                secondary_mode == "linear".
            secondary_current_limit: Optional source current compliance limit
                in A for the secondary channel during the synchronized block.

        Yields:
            Tuples
            (
                primary_source_voltages,
                primary_currents,
                primary_measured_voltages,
                secondary_source_voltages,
                secondary_currents,
                secondary_measured_voltages,
                primary_timestamps,
            )
            for each chunk.
        """
        pass
