"""`gate: narration`: does a passage the change writes narrate history, attempts or verification instead of stating what is?

The rule is AGENTS.md's "Every artifact is as-built", which opens the
section the gate quotes. The items are the passages of prose the change
writes (`prose`). Every path can carry a comment, so every change triggers
the gate, and one that writes no prose gives it no item to ask about.
"""

from __future__ import annotations

from ..gate import Canon, Spec
from .prose import extract

GATE = Spec(canon=Canon('AGENTS.md', 'Writing the prose'), trigger=('**',), extract=extract)
