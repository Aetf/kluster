"""The lists of a resource's state that hold sets, and a state read by their members.

The engine writes those lists in a fresh order on every registration
(`DEPENDENCIES`), so a comparison of two states that looks for a change of
members, and not of order, reads each one through `by_members`.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import cast

#: The field of a resource's state that lists a set, and the field that maps
#: each input to one. The engine gathers their members in a set and lists them
#: in its map's iteration order (`pkg/resource/deploy/source_eval.go`), so two
#: registrations of the same members list them in different orders, and no
#: `up` settles one. They compare by their members; every other list in a
#: state, those inside `inputs` among them, is a value whose order counts.
DEPENDENCIES = 'dependencies'
PROPERTY_DEPENDENCIES = 'propertyDependencies'


def by_members(state: Mapping[str, object]) -> Mapping[str, object]:
    """`state` with `DEPENDENCIES` and each list of `PROPERTY_DEPENDENCIES` in one order, and each member once."""
    ordered = dict(state)
    if isinstance(listed := ordered.get(DEPENDENCIES), list):
        ordered[DEPENDENCIES] = _members(cast('list[object]', listed))
    if isinstance(mapped := ordered.get(PROPERTY_DEPENDENCIES), dict):
        ordered[PROPERTY_DEPENDENCIES] = {
            key: _members(cast('list[object]', value)) if isinstance(value, list) else value
            for key, value in cast('dict[str, object]', mapped).items()
        }
    return ordered


def _members(listed: list[object]) -> list[object]:
    """The members of `listed`, each once, in the order of their JSON."""
    by_json = {json.dumps(member, sort_keys=True): member for member in listed}
    return [by_json[key] for key in sorted(by_json)]
