"""§3.4's capacity reading, as the operator pastes it from Cloud Shell.

The reading runs under the operator's own identity, since a capacity report's
compartment is the tenancy's root, which `physical`'s key cannot reach
(operations.md §3.4). So the drill applies the pass rule to what the operator
pastes and records when it was taken; it makes no call of its own.

Each line is an availability domain, its `availability-status` and its
`available-count`. **An AD passes at `AVAILABLE`, with a count of at least 1
where a count is given**; for `VM.Standard.A1.Flex` the count comes back
`null`. A deliberate run terminates only within `FRESH` of a reading in which
every AD passed, a line for the domain of every instance in state among them.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Collection, Sequence
from dataclasses import dataclass
from typing import cast

#: How old a reading may be when the nodes are terminated (operations.md §3.5).
FRESH = dt.timedelta(hours=1)

AVAILABLE = 'AVAILABLE'


@dataclass(frozen=True)
class Domain:
    name: str
    status: str
    #: None where the report gives no count.
    count: int | None

    @property
    def passes(self) -> bool:
        return self.status == AVAILABLE and (self.count is None or self.count >= 1)


@dataclass(frozen=True)
class Capacity:
    domains: tuple[Domain, ...]
    taken: dt.datetime

    def refusal(self, at: dt.datetime, required: Collection[str]) -> str | None:
        """Why the nodes may not be terminated at `at` on this reading, or None where they may.

        `required` is the availability domain of every instance in state: a
        reading that leaves one out says nothing about the create that
        domain is asked for.
        """
        if not self.domains:
            return 'the reading lists no availability domain'
        if at - self.taken > FRESH:
            return f'the reading was taken at {self.taken:%H:%M:%S}Z, more than {FRESH} before now'
        missing = sorted(set(required) - {each.name for each in self.domains})
        if missing:
            return f'the reading leaves out {", ".join(missing)}, where an instance in state sits'
        failing = [f'{each.name} {each.status} {each.count}' for each in self.domains if not each.passes]
        return f'not every AD passes: {"; ".join(failing)}' if failing else None

    def dump(self) -> dict[str, object]:
        return {
            'taken': self.taken.isoformat(),
            'domains': [[each.name, each.status, each.count] for each in self.domains],
        }

    @classmethod
    def load(cls, kept: dict[str, object]) -> Capacity:
        rows = cast('list[list[object]]', kept['domains'])
        domains = [
            Domain(str(name), str(status), None if count is None else int(str(count))) for name, status, count in rows
        ]
        return cls(tuple(domains), dt.datetime.fromisoformat(str(kept['taken'])))


class Unreadable(ValueError):
    """A pasted line that is not `<AD> <status> <count>`."""


def parse(lines: Sequence[str], taken: dt.datetime) -> Capacity:
    """The reading the operator pasted, one AD per line."""
    domains: list[Domain] = []
    for line in lines:
        fields = line.split()
        if not fields:
            continue
        if len(fields) != 3:
            raise Unreadable(f'{line!r} is not `<AD> <status> <count>`')
        name, status, count = fields
        if any(each.name == name for each in domains):
            raise Unreadable(f'{name} is pasted twice')
        if count != 'null' and not count.isdigit():
            raise Unreadable(f'{line!r}: the count is neither a number nor null')
        domains.append(Domain(name, status, None if count == 'null' else int(count)))
    return Capacity(tuple(domains), taken)
