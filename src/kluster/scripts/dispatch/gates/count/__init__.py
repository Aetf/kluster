"""`gate: count`: does a count the change writes have its members in view, and is it its own document's to keep?

The canon is style/README.md's "A count stands beside the members it
counts", which holds the count rules. Every path can carry a comment, so
every change triggers the gate, which is shown each file the change writes
and each of its commits' messages, and asked of each count it writes into
prose, a comment, a docstring or a message whether the rules allow it where
it stands.
"""

from __future__ import annotations

from ..gate import Canon, Spec

GATE = Spec(canon=Canon('docs/style/README.md', 'A count stands beside the members it counts'), trigger=('**',))
