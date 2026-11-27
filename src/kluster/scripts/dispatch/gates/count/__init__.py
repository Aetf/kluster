"""`gate: count`: does a count the change writes have the members it counts in view?

The canon is style/README.md's "A count stands beside the members it
counts": where a count may stand, in prose and in a comment. A count
restated from another document is the rule of the section after it, and
not this gate's question. Every path can carry a comment, so every change
triggers the gate, which is shown each file the change writes and each of
its commits' messages, and asked of each count it writes into prose, a
comment, a docstring or a message whether the rule allows it where it
stands.
"""

from __future__ import annotations

from ..gate import Canon, Spec

GATE = Spec(canon=Canon('docs/style/README.md', 'A count stands beside the members it counts'), trigger=('**',))
