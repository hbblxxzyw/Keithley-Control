"""
Instrument abstract base class module.

Provides the AbstractSMU abstract base class as a unified interface template
for all real and virtual instruments.
"""

from abc import ABC, abstractmethod


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
    ) -> tuple[list[float], list[float]]:
        """
        Run a linear voltage sweep (IV sweep) on the specified channel.

        Args:
            smu_channel: Channel identifier, either 'smua' or 'smub'.
            start_v: Start voltage in V.
            stop_v: Stop voltage in V.
            points: Number of sweep points (including endpoints); voltage
                is linearly spaced between start_v and stop_v.

        Returns:
            Tuple (voltages, currents):
            - voltages: List of voltages, length equals points.
            - currents: List of corresponding currents, length equals points.
        """
        pass
