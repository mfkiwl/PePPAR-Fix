"""Abstract interfaces for phase measurement and frequency actuation.

These ABCs decouple the servo loop from specific hardware. Current
implementations: PhcAdjfineActuator (Linux PHC), ClockMatrixActuator
(Renesas 8A34012 I2C). Future: White Rabbit, other timing chips.
"""

from abc import ABC, abstractmethod


class FrequencyActuator(ABC):
    """Adjusts the output frequency of a clock source."""

    @abstractmethod
    def setup(self) -> None:
        """Prepare hardware for frequency control (e.g., DPLL mode switch)."""

    @abstractmethod
    def teardown(self) -> None:
        """Restore hardware to default state."""

    @abstractmethod
    def adjust_frequency_ppb(self, ppb: float) -> float:
        """Apply absolute frequency offset in ppb. Returns actual applied ppb."""

    @abstractmethod
    def read_frequency_ppb(self) -> float:
        """Read current frequency offset in ppb."""

    @property
    @abstractmethod
    def max_adj_ppb(self) -> float:
        """Maximum frequency adjustment magnitude."""

    @property
    @abstractmethod
    def resolution_ppb(self) -> float:
        """Smallest distinguishable frequency step in ppb."""


class PhaseSource(ABC):
    """Reads phase error between a reference and a steered clock."""

    @abstractmethod
    def setup(self) -> None:
        """Prepare hardware for phase measurement."""

    @abstractmethod
    def teardown(self) -> None:
        """Release hardware resources."""

    @abstractmethod
    def read_phase_ns(self) -> float | None:
        """Read current phase error in nanoseconds.

        Returns None if measurement is unavailable.
        Sign: positive = steered clock is late (needs to speed up).
        """

    @property
    @abstractmethod
    def resolution_ns(self) -> float:
        """Nominal single-shot resolution in nanoseconds."""
