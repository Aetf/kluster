"""`gate: narration`: does a passage the change writes narrate history, attempts, verification or provenance instead of stating what is?

The rule is AGENTS.md's "Every artifact is as-built", the section the gate
quotes. Every path can carry a comment, so every change triggers the gate,
which is shown each file the change writes and each of its commits'
messages, since a commit message is an artifact the rule holds as it holds
a file.
"""

from __future__ import annotations

from ..gate import Canon, Spec

GATE = Spec(canon=Canon('AGENTS.md', 'Every artifact is as-built'), trigger=('**',))
