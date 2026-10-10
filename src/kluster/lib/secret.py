"""The marker a field carrying credential material is declared with.

A record field that holds a credential is declared `Annotated[<its type>,
Secret]` — `Annotated[str, Secret]`, `Annotated[pulumi.Input[str], Secret]` —
and kept out of the record's repr and out of its comparison:

    password: Annotated[str, Secret] = field(repr=False, compare=False)

**The marker says what the field is; the two annotations are what protect
it**, and `tests/test_secret_reprs.py` holds every marked field in the package
to both, reading the fields off the marker rather than off a list. Each
annotation covers a different printer. `repr=False` keeps the value out of the
record's repr, which a log line's `%r` and the first line of a failed
assertion print. `compare=False` keeps it out of pytest's explanation of a
failed `==` between two records, which drills into every compared field
whatever its repr. A record is a dataclass here, because a `NamedTuple` or a
`TypedDict` has no per-field repr control, and so may carry no marked field at
all.

**The marker changes nothing the type checker sees.** `Annotated[str,
Secret]` is `str` to it, so the field takes what it took before and its
readers read a `str`. A distinct type would let the checker refuse something,
but not the thing that leaks: a `NewType` over `str` refuses a bare `str` into
a marked field, which is harmless, and accepts a marked value into an unmarked
`str` field, because it is a subtype of `str` — and that is the leak. A
wrapper that is not a `str` would refuse that direction too, but only once
every reader unwraps it and every credential arrives wrapped from its source,
and no single such type spans what credential fields hold: strings, Pulumi
inputs, mappings of either, and library objects that carry a key inside them.
The marker spans all of them.

**What nothing here stops is a credential in a field nobody marked.** Neither
the checker nor the case can tell an unmarked `str` that holds a password from
one that holds a host name, so a new secret field is held to the two
annotations from the commit that marks it, and not before.
"""

from __future__ import annotations

from typing import final


@final
class Secret:
    """The `Annotated` metadata that marks a field as carrying credential material.

    Used as the class itself, never instantiated: what is read is whether the
    field's declared type names it, and what marks a field is that its author
    wrote it there.
    """
