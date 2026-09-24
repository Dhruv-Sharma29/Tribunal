"""Back-off schedule for the delivery worker."""

from dataclasses import dataclass


@dataclass(frozen=True)
class Schedule:
    """An exponential back-off, capped."""

    base_seconds: float = 1.0
    factor: float = 2.0
    max_seconds: float = 60.0

    def delay_for(self, attempt: int) -> float:
        """Seconds to wait before `attempt`, which is 1-based.

        Capped at `max_seconds`, so a long-lived failure does not schedule a retry
        days out.
        """
        if attempt < 1:
            raise ValueError("attempt is 1-based")
        return min(self.base_seconds * self.factor ** (attempt - 1), self.max_seconds)

    def total_for(self, attempts: int) -> float:
        """Total time spent waiting across `attempts` retries."""
        return sum(self.delay_for(n) for n in range(1, attempts + 1))
