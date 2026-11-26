"""`gate: enumeration`: does this name a set's members where a definition would cover them?

The canon is style/README.md, "A membership is a definition, not a list".
The gate is shown each file the change writes under `src/` and `tests/`
and in the style rules, and every brief (`TRIGGER`). What the model is
asked is whether a passage the change writes names members where a
definition would take the next one, so that the next member needs an edit
there; a definition, today's instances after one, and a census nothing can
derive pass.
"""

from __future__ import annotations

from ..gate import Canon, Spec

#: The program and the tests, the style rules, and every brief.
TRIGGER = ('src/**', 'tests/**', 'docs/style/**', ':brief/**')

GATE = Spec(canon=Canon('docs/style/README.md', 'A membership is a definition, not a list'), trigger=TRIGGER)
