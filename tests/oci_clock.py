"""The clock `oci_iam`'s propagation waits run on, in every suite that reaches them.

`oci_iam` outwaits two things on a deadline: a fresh key that does not
authenticate yet, and a delete the service refuses for a while
(`PROPAGATION_DEADLINE`, polled every `PROPAGATION_INTERVAL`). On the real
clock, a refusal a fake keeps answering costs real seconds toward a deadline
past the suite's per-case bound, and the case fails as a `pytest-timeout`
stopped inside `time.sleep` rather than at the refusal it is about
(testing.md §7 item 5). So every suite that reaches those waits -- to
exercise them, or only to mint through them -- installs this clock for the
whole module, under the fixture name `unhurried`; and code that reaches them
at import, before any fixture exists, installs it in a
`pytest.MonkeyPatch.context()` of its own.

What it replaces is `oci_iam`'s own name `time`, not the `time` module: the
waits are the only thing in that module that reads a clock, and everything
else a suite runs -- a subprocess's poll loop, the per-case bound itself --
keeps the real one.
"""

# The SDK ships no stubs; the same waiver `oci_iam.py` itself carries.
# pyright: reportMissingTypeStubs=false

from __future__ import annotations

from dataclasses import dataclass, field
from typing import cast

import oci
import pytest

from kluster.scripts.credentials import oci_iam


@dataclass
class SimulatedClock:
    """A clock only the wait moves: each `sleep` advances it by what was asked, and is recorded.

    A deadline is therefore reached after exactly the sleeps the wait would
    have spent, without a second of wall time, and `slept` is what a case
    asserts how many intervals were spent with.
    """

    now: float = 0.0
    slept: list[float] = field(default_factory=list[float])

    def monotonic(self) -> float:
        return self.now

    def sleep(self, interval: float) -> None:
        self.slept.append(interval)
        self.now += interval


def install(monkeypatch: pytest.MonkeyPatch) -> SimulatedClock:
    """Put `oci_iam`'s waits on a fresh simulated clock for the length of `monkeypatch`."""
    clock = SimulatedClock()
    monkeypatch.setattr(oci_iam, 'time', clock)
    return clock


@dataclass
class _RefusesOnce:
    """An `Iam` whose one delete is refused the first time and taken the second."""

    refused: bool = False

    def retire_key(self, user_id: str, key_fingerprint: str) -> None:
        if not self.refused:
            self.refused = True
            raise oci.exceptions.ServiceError(
                status=401, code='NotAuthenticated', headers=dict[str, str](), message='not yet'
            )


def one_refusal_outwaited() -> list[float]:
    """Drive one of `oci_iam`'s waits through one refusal, and return what it slept on the simulated clock.

    The wait runs first and the clock is read after it, so a suite whose waits
    have gone back to the real clock pays the interval in real seconds before
    it fails here, by name.
    """
    oci_iam._retire(cast('oci_iam.Iam', _RefusesOnce()), 'a-user', 'a-key')  # pyright: ignore[reportPrivateUsage]
    clock: object = vars(oci_iam)['time']
    assert isinstance(clock, SimulatedClock), 'oci_iam waits on the real clock here; install `oci_clock` module-wide'
    return clock.slept
