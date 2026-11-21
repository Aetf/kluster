"""The URNs a mocked run's registrations carry, spelled as the engine spells them.

The mock qualifies a type by the immediate parent's type alone
(framework/testing.md §3.1), while the engine qualifies it by the whole chain
of parents' types, the stack's own aside. A pattern a runbook or a script
matches against `pulumi stack --show-urns` is matched against these.
`test_physical_stack.py` and `test_drill.py` read them (framework/testing.md
§2).
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import yaml

#: The project every URN of a real run carries, `Pulumi.yaml`'s `name`.
PROJECT = cast('dict[str, Any]', yaml.safe_load((Path(__file__).parent.parent / 'Pulumi.yaml').read_text()))['name']

#: The type of the stack's own resource, which qualifies no type below it.
ROOT = 'pulumi:pulumi:Stack'


def engine_urns(registrations: Mapping[str, Any], *, stack: str = 'physical') -> dict[str, str]:
    """Each registration's URN, by the mock's URN, as the engine spells it."""

    def qualified(urn: str) -> str:
        request = registrations[urn]
        above = registrations.get(request.parent)
        if above is None or above.type == ROOT:
            return str(request.type)
        return f'{qualified(request.parent)}${request.type}'

    return {urn: f'urn:pulumi:{stack}::{PROJECT}::{qualified(urn)}::{registrations[urn].name}' for urn in registrations}
