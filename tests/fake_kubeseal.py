"""A `kubeseal` that seals nothing, for the cases about where a ciphertext lands and what it was sealed from.

`sealing.Sealer` runs `kubeseal` through the `Kubeseal` it is handed, once
for each key, with the key's plaintext on standard input. This stand-in
answers each run with a ciphertext that names that plaintext,
`AgB-sealed-<plaintext>`, so a case that reads back what was written can
tell which value reached which place. What `kubeseal` itself makes of its
arguments is held against the real binary in `tests/test_sealing.py`.
"""

from __future__ import annotations

from collections.abc import Sequence


def sealed_as_marked(args: Sequence[str], *, stdin: str | None) -> str:
    """Seals nothing: a ciphertext stand-in that names what it was given, so a seal reaches the write it feeds."""
    del args
    return f'AgB-sealed-{stdin}\n'
