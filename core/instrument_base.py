"""
Instrument abstract base class module.

Provides the AbstractSMU abstract base class as a unified interface template
for all real and virtual instruments.
"""

from abc import ABC, abstractmethod
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
            current_limit: Optional current compliance/range hint in A for
                configuring a fixed current measurement range during the sweep.
            ramp_up: If True, ramp from 0 V to start_v before the sweep.
            ru_step: Ramp-up voltage step in V.
            ru_delay: Ramp-up step delay in s.
            ramp_down: If True, ramp from stop_v to 0 V after the sweep.
            rd_step: Ramp-down voltage step in V.
            rd_delay: Ramp-down step delay in s.

        Yields:
            Tuples (voltages, currents) for each chunk:
            - voltages: List of actual source voltages (may include ramp points).
            - currents: List of corresponding currents, same length as voltages.
        """
        pass
