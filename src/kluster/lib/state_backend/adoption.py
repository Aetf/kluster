"""The cutover's adoption: which resources the `state-backend` stack imports, and where their ids are kept.

The appliance that serves when the stack is first applied was built by a
script, so every resource the stack keeps but the box already exists. `state-
backend adopt` reads their ids once and writes them into the stack's
configuration under `KEY`, as one JSON object in the clear; the stack program
reads it and the component passes each id to its resource as the `import_`
option, so each comes into the committed state through the program, with the
program's secret markings (framework/pulumi.md §3.3).

**Three resources are never adopted.** The instance: an import runs no
program, so its `metadata`, with the keys its Ignition carries, would be
recorded in the clear, and the box is replaced instead (rfc-006 §3.6). The dump
key the script minted, whose secret B2 returned once: the stack mints its own.
And the image: OCI returns no image's source, so the imported image would
differ from its declaration and be replaced, and the engine refuses to replace
a resource whose declaration still carries an import id. The stack imports its
own image instead. The script's key and image are deleted by hand.

**What is adopted is all of it or none.** A name missing from `KEY` is a
resource the program would create beside the one that exists -- a second VCN,
a second reserved address the gate cannot see -- so `read` refuses a value that
names some of `ADOPTABLE` and not the rest.

Here because the command that writes the ids and the program that reads them
both name them, and neither imports the other (style/pulumi.md, "Layering").
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import cast

#: The configuration key the ids are kept under, bare and so in the project's
#: namespace, as every key the stack program reads is.
KEY = 'adopted'

#: Each resource the stack adopts, as the configuration's object names it.
VCN = 'vcn'
GATEWAY = 'gateway'
SUBNET = 'subnet'
IMAGE_BUCKET = 'imageBucket'
ADDRESS = 'address'
DUMP_BUCKET = 'dumpBucket'

#: Every name `KEY` may hold, in the order `adopt` reads them.
ADOPTABLE = (VCN, GATEWAY, SUBNET, IMAGE_BUCKET, ADDRESS, DUMP_BUCKET)


class Refused(ValueError):
    """What the configuration holds under `KEY` is not a set of ids the stack can import."""


def read(value: object) -> dict[str, str]:
    """The ids `value` holds, keyed by the names in `ADOPTABLE`; `{}` for no value, or an empty one.

    Refused by name where the value is not an object of non-empty strings,
    names a resource the stack does not adopt -- an id the program would set
    on no resource is an edit that went wrong, not one to pass over -- or
    leaves out a name the stack does adopt.
    """
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise Refused(f'the {KEY!r} configuration holds a {type(value).__name__}, not an object of ids')
    found: dict[str, str] = {}
    for name, held in cast('Mapping[object, object]', value).items():
        if name not in ADOPTABLE:
            raise Refused(f'the {KEY!r} configuration names {name!r}, which is none of {", ".join(ADOPTABLE)}')
        if not isinstance(held, str) or not held:
            raise Refused(f'the {KEY!r} configuration holds {held!r} for {name}, not an id')
        found[str(name)] = held
    if found and (missing := [name for name in ADOPTABLE if name not in found]):
        raise Refused(
            f'the {KEY!r} configuration names no id for {", ".join(missing)}: the program would create each '
            'beside the one that exists. `state-backend adopt` writes every one'
        )
    return found
