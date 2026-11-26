"""`gate: falsified-claim`: given what a change did, is a passage elsewhere now false or incomplete?

The canon is style/README.md's "A sweep looks for the claim, not the
identifier", which says how a claim a change makes false is found: by the
claim's nouns, close together over whitespace-collapsed text, rather than by
the identifier that moved. The extractor (`extract`) runs that sweep at the
head on every pull request. Its patterns are the description's own, from its
`## Sweep` section, and noun pairs it draws from each hunk the change makes,
on both sides of it (`sweep`). It reads the prose (`gates.prose`) of every
tracked file this repository writes (`extract.written_here`), so a hit is a
paragraph, a comment or a docstring, the places a claim is written. A hit
that lies within the hunk its pattern came from is the claim restated in
place, and is left out. Every other hit is an item, with the hunks behind it
as context, so the model is asked only about passages the change did not
write itself.
"""

from __future__ import annotations

from ..gate import Canon, Spec
from .sweep import extract

GATE = Spec(
    canon=Canon('docs/style/README.md', 'A sweep looks for the claim, not the identifier'),
    trigger=('**',),
    extract=extract,
)
