"""Renovate's uv is the one `mise.toml` pins, so the two never rewrite each other's `uv.lock`.

Every lock change made by hand runs the pinned uv; lock file maintenance and
a Python bump's lock run whatever uv renovate installs. A uv release writes
the lock revision it defines, and releases have moved it, so the next lock
change made by the other uv moves the revision back: a diff that reads as a
change in review. `renovate.json5` constrains renovate's uv to the pin, and
groups the constraint with the pin, so one release moves both in one pull
request.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

from renovate_text import group, listed, package_rules, scalar

ROOT = Path(__file__).parent.parent


def test_renovates_uv_is_the_pinned_one_and_moves_with_it() -> None:
    """Held as text, because there is no JSON5 parser here (`renovate_text`)."""
    config = (ROOT / 'renovate.json5').read_text()
    pinned = tomllib.loads((ROOT / 'mise.toml').read_text())['tools']['uv']

    # The constraint is the pin.
    constrained = re.findall(r"^  constraints: \{\n    uv: '([^']+)',\n  \},$", config, re.MULTILINE)
    assert constrained == [pinned], (
        f"renovate's uv is {constrained or 'unconstrained, so the newest'}; mise.toml pins {pinned}"
    )

    # The manager that reads the constraint reads it from the releases the
    # mise pin is read from, and puts it in the pin's pull request: the group
    # the rule for the mise manager names.
    rules = package_rules(config)
    (constraint,) = [rule for rule in rules if listed(rule, 'matchManagers') == ['renovate-config']]
    assert listed(constraint, 'matchDepNames') == ['uv']
    assert (scalar(constraint, 'overrideDatasource'), scalar(constraint, 'overridePackageName')) == (
        'github-releases',
        'astral-sh/uv',
    )
    toolchain = next(rule for rule in rules if listed(rule, 'matchManagers') == ['mise'])
    assert group(constraint) == group(toolchain)
