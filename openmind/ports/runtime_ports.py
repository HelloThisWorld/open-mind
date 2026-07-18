"""Runtime seams that make bounded waiting testable.

:class:`Clock` exists because :class:`~openmind.services.job_service.JobService`
polls persisted job state on a timeout. Testing "a 30-second wait times out"
against the real clock would mean a 30-second test; against a fake clock it is
instant and deterministic. That is a genuine second implementation, not an
abstraction for its own sake.

Nothing else in the runtime is abstracted here. Configuration, the filesystem
and the database are used directly — they already have a test seam
(``OPENMIND_DATA_DIR`` / ``OPENMIND_MACHINE_DIR``) that the whole existing
suite relies on, and wrapping them would add indirection without adding
coverage.
"""
from __future__ import annotations

import time
from typing import List, Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """Monotonic time + sleep, so bounded waits can be driven deterministically."""

    def monotonic(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


class SystemClock:
    """The real clock. Uses ``time.monotonic`` so a system clock adjustment
    mid-wait cannot make a timeout fire early or hang."""

    def monotonic(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)


class FakeClock:
    """A clock that advances only when slept on.

    Turns "poll until timeout" into a deterministic, instant test. Records every
    sleep so a test can assert the poll interval as well as the outcome.
    """

    def __init__(self, start: float = 0.0) -> None:
        self._now = float(start)
        self.sleeps: List[float] = []

    def monotonic(self) -> float:
        return self._now

    def sleep(self, seconds: float) -> None:
        self.sleeps.append(float(seconds))
        self._now += float(seconds)


__all__ = ["Clock", "SystemClock", "FakeClock"]
