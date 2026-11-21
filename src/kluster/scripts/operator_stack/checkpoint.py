"""What a run of a stack whose state is committed is checked for (framework/pulumi.md §3.3).

The checkpoint is a tracked file of this repository, and **the publication is
the push**: a branch carrying it is public from the moment it reaches the
forge, before any review. So the checks here run on the workstation, where the
file is still private:

-   **Before a run**, that the working copy holds the forge's current `main`,
    as every operator stack's run is checked for (`manual_run.require_main`),
    and then that the checkpoint is not conflicted (`require_current`). A run
    from an older checkpoint plans against a state that is not the latest one.
-   **After a run**, that the file holds no secret value in the clear
    (`cleartext`), and that every property the engine marks secret is
    ciphertext wherever the file records it (`undeclared`). Both name the
    property they found and never the value. A run that can write is
    recorded as unchecked before it starts (`record`), and the record comes
    off only when the checks pass.
-   **After a run**, too, that the stack has no file in the backend's
    directory but its checkpoint and that checkpoint's `.bak` (`strays`): a
    compressed or retained copy is a file the checks never read.
-   **Around a run**, that a run which changed no part of the deployment
    leaves the file's bytes as they were (`same_deployment`), since every
    write moves the manifest's timestamp and re-encrypts every secret under a
    fresh nonce.
"""

from __future__ import annotations

import copy
import enum
import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from kluster.lib import dependency_sets, stack_environment
from kluster.scripts import manual_run

#: The two keys of Pulumi's secret envelope: the signature key, and the value
#: it holds in every envelope. A file carries the value encrypted beside them
#: as `ciphertext`; `pulumi stack export --show-secrets` carries it decrypted,
#: JSON-encoded, as `plaintext`.
SECRET_SIG = '4dabf18193072939515e22adb298388d'
SECRET_MARK = '1b47061264138c4ac30d75fd1eb44270'

#: The shortest string searched for as a secret. A value this short carries too
#: little to be told apart from ordinary text in the file, so searching for it
#: would find it everywhere; the secrets this repository keeps are keys and
#: tokens, many times longer.
SHORTEST = 8


class Kind(enum.Enum):
    """What a finding says the checkpoint would publish."""

    #: A secret's value, outside any ciphertext envelope.
    CLEARTEXT = 'cleartext'
    #: An input named in `additionalSecretOutputs` that is not ciphertext.
    DECLARED_INPUT = 'declared input'
    #: An output named in `additionalSecretOutputs` that is not ciphertext.
    DECLARED_OUTPUT = 'declared output'
    #: An output whose input of the same name holds ciphertext, and which does not.
    CARRIED = 'carried'
    #: A secret envelope holding its plaintext rather than a ciphertext.
    PLAINTEXT = 'plaintext'
    #: A file for the stack the checks do not read.
    STRAY = 'stray'


@dataclass(frozen=True)
class Finding:
    """A property of the checkpoint that must not be published as it stands.

    `where` names the place; `sources`, for cleartext, names whose values are
    there; `input_to_mark` names the program's input that clears it, passed
    whole as a secret: that input is one envelope at the next write, and the
    engine then wraps the output of its name too (framework/pulumi.md §3.3).
    """

    where: str
    kind: Kind
    sources: tuple[str, ...] = ()
    input_to_mark: str | None = None

    @property
    def what(self) -> str:
        """The finding as the operator reads it."""
        match self.kind:
            case Kind.CLEARTEXT:
                said = f'holds the value of {", ".join(self.sources)} in the clear'
            case Kind.DECLARED_INPUT | Kind.DECLARED_OUTPUT:
                said = 'is named in additionalSecretOutputs and is not ciphertext'
            case Kind.CARRIED:
                said = 'holds ciphertext as an input and is not ciphertext as an output'
            case Kind.PLAINTEXT:
                said = 'is a secret envelope holding its plaintext'
            case Kind.STRAY:
                said = 'is not a file the checks read, and must not be pushed'
        if self.input_to_mark is not None:
            said += f'; pass the input `{self.input_to_mark}` whole as `pulumi.Output.secret(...)`'
        return said

    def __str__(self) -> str:
        return f'{self.where}: {self.what}'


# --------------------------------------------------------------------------
# Before a run: the working copy.
# --------------------------------------------------------------------------


def require_current(checkout: Path, checkpoint: Path | None) -> None:
    """Refuse a run from a working copy behind the forge's `main`, or over a conflicted checkpoint.

    **The ancestry comes first** (`manual_run.require_main`), because of what
    the conflict's remedy does. After a checkpoint lands, the fetch that
    abandons the landed change can leave a second, unlanded run's checkpoint
    conflicted in the working copy until the rebase resolves it; checked
    first, the conflict's remedy would throw that unlanded run away, where the
    ancestry's refusal sends the operator to the rebase that resolves it.
    """
    manual_run.require_main(checkout)
    if checkpoint is not None and conflicted(checkpoint):
        remedy: manual_run.Remedy = (('jj', 'restore', '--from', 'main', str(checkpoint.relative_to(checkout))),)
        raise manual_run.CheckRefused(
            f"{checkpoint} is conflicted: two runs wrote it from one base. Keep main's side "
            f'({manual_run.spelled(remedy)}), run a refreshed `plan`, and bring in '
            "through the program's `import_` what the other run created, or delete it by hand. An instance "
            'the other run created is terminated by hand, never imported.',
            why=manual_run.Unready.CONFLICTED,
            remedy=remedy,
        )


def conflicted(checkpoint: Path) -> bool:
    """Whether `checkpoint` holds conflict markers.

    Read from the file, so the answer is the same whichever tool left them: a
    `jj` conflict and a `git` merge both write lines opening with seven or
    more `<`, and the checkpoint Pulumi writes is one line of JSON that opens
    with `{`.
    """
    return any(line.startswith('<<<<<<<') for line in checkpoint.read_text().splitlines())


# --------------------------------------------------------------------------
# Around a run: an unchanged deployment keeps its bytes.
# --------------------------------------------------------------------------


def same_deployment(before: Mapping[str, Any], after: Mapping[str, Any]) -> bool:
    """Whether two `pulumi stack export --show-secrets` documents hold the same deployment.

    Compared without the manifest's timestamp, which every write moves; with
    the resources in one canonical order, since the engine does not keep
    theirs between runs when resources register concurrently; and with each
    resource state's dependency lists by their members
    (`dependency_sets.by_members`), since the engine lists those in a fresh
    order on every registration -- a resource's own, and the one a pending
    operation holds. The export carries each secret's envelope beside its
    plaintext rather than a ciphertext, so a re-encryption under a fresh
    nonce is no difference, and a property that became secret is one.
    """
    return _normal(before) == _normal(after)


def _normal(export: Mapping[str, Any]) -> object:
    document = copy.deepcopy(dict(export))
    deployment = cast('dict[str, Any]', document.get('deployment') or {})
    manifest = cast('dict[str, Any]', deployment.get('manifest') or {})
    _ = manifest.pop('time', None)
    resources = [_state_by_members(resource) for resource in cast('list[object]', deployment.get('resources') or [])]
    deployment['resources'] = sorted(resources, key=lambda resource: json.dumps(resource, sort_keys=True))
    for operation in cast('list[object]', deployment.get('pending_operations') or []):
        if isinstance(operation, dict) and 'resource' in operation:
            held = cast('dict[str, object]', operation)
            held['resource'] = _state_by_members(held['resource'])
    return document


def _state_by_members(state: object) -> object:
    """`state` with its dependency lists by their members, where it is a state at all."""
    return dependency_sets.by_members(cast('dict[str, object]', state)) if isinstance(state, dict) else state


# --------------------------------------------------------------------------
# After a run: what the file would publish.
# --------------------------------------------------------------------------


def envelope(value: object) -> bool:
    """Whether `value` is a secret envelope, encrypted or exported."""
    return isinstance(value, dict) and cast('dict[str, object]', value).get(SECRET_SIG) == SECRET_MARK


def _member(document: Mapping[str, Any], *keys: str) -> Any:
    """`document[keys[0]][keys[1]]…`, or None where a level is absent or null."""
    value: Any = document
    for key in keys:
        value = cast('dict[str, Any]', value).get(key) if isinstance(value, dict) else None
    return value


def resources(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    """The resources a checkpoint file records; none before the stack's first update."""
    return cast('list[dict[str, Any]]', _member(document, 'checkpoint', 'latest', 'resources') or [])


def export_secrets(export: Mapping[str, Any]) -> dict[str, str]:
    """Every secret value a `--show-secrets` export holds, as the text searched for, by where it is."""
    found: dict[str, str] = {}
    for resource in cast('list[dict[str, Any]]', _member(export, 'deployment', 'resources') or []):
        for path, value in _envelopes(resource, str(resource.get('urn', '?')), ''):
            plaintext = cast('dict[str, object]', value).get('plaintext')
            if isinstance(plaintext, str):
                for index, text in enumerate(_strings(json.loads(plaintext))):
                    found[f'{path}#{index}' if index else path] = text
    return found


def config_secrets(config: Mapping[str, Any]) -> dict[str, str]:
    """Every secret value in `pulumi config --show-secrets --json`, by key."""
    found: dict[str, str] = {}
    for key, entry in config.items():
        if not isinstance(entry, dict) or not cast('dict[str, object]', entry).get('secret'):
            continue
        entry = cast('dict[str, object]', entry)
        value = entry.get('objectValue', entry.get('value'))
        for index, text in enumerate(_strings(value)):
            found[f'config {key}#{index}' if index else f'config {key}'] = text
    return found


def needles(secrets: Mapping[str, str]) -> list[tuple[str, str]]:
    """What is searched for: each value whole, and each line of a multi-line one, with where it came from.

    Line by line because a multi-line value such as a key can land in the file
    re-wrapped inside another string, where the whole never appears and every
    line of it does. A line of PEM armor is a label rather than a secret, and a
    line shorter than `SHORTEST` is too short to identify, so neither is
    searched for on its own.
    """
    found: list[tuple[str, str]] = []
    for source, value in secrets.items():
        if len(value) >= SHORTEST:
            found.append((value, source))
        if '\n' in value.strip():
            for line in value.splitlines():
                line = line.strip()
                if len(line) >= SHORTEST and not line.startswith('-----'):
                    found.append((line, source))
    return found


def cleartext(document: Mapping[str, Any], secrets: Mapping[str, str]) -> list[Finding]:
    """Every place outside a ciphertext envelope where `document` holds one of `secrets`, once per place.

    The search is literal: a value is found where its text is, and not where
    it was encoded first — in base64, in hex, or escaped inside a string that
    is itself a JSON document.
    """
    searched = needles(secrets)
    sources: dict[str, dict[str, None]] = {}
    for where, text in _texts(document):
        for needle, source in searched:
            if needle in text:
                sources.setdefault(where, {})[source] = None
    return [Finding(where, Kind.CLEARTEXT, sources=tuple(found)) for where, found in sources.items()]


def undeclared(document: Mapping[str, Any]) -> list[Finding]:
    """Every place `document` records in the clear a property the engine marks secret.

    Read from the file alone, so it needs no value to check. The engine marks
    a property secret by two rules at the pinned CLI (framework/pulumi.md
    §3.3 cites the source), and every resource state the file records
    (`recorded`) is held to both:

    -   **A key the resource's options name in `additionalSecretOutputs`.**
        The engine wraps that output whole. It leaves the input of the same
        name as the program passed it, which is the program's to mark, and
        the check holds it to ciphertext too.
    -   **An output whose input of the same name holds ciphertext anywhere
        in it.** Where both are objects the engine recurses into them;
        otherwise it wraps the whole output (`_carried`). The engine applies
        the recursion only for a provider that does not accept secrets, and
        the file does not record which provider did, so a provider that
        accepts them is held to it as well.

    In the clear means anything but one envelope or a null: what the engine
    marks it wraps whole, so a structure with envelopes inside it is refused,
    whether the engine's marking was lost at its top or a provider that
    accepts secrets marked only inside it. Either input rule is cleared the
    same way, by the program passing that input whole as a secret, and the
    refusal says which input.

    An envelope that holds its `plaintext` rather than a `ciphertext` is the
    value in the clear: the form `pulumi stack export --show-secrets` writes,
    which the engine loads from a checkpoint as readily as the encrypted one.

    A place is named once, by the first rule that finds it.
    """
    findings: dict[str, Finding] = {}
    for owner, resource in recorded(document):
        inputs = cast('dict[str, object]', resource.get('inputs') or {})
        outputs = cast('dict[str, object]', resource.get('outputs') or {})
        found: list[Finding] = []
        for key in cast('list[str]', resource.get('additionalSecretOutputs') or []):
            if key in inputs and _clear(inputs[key]):
                found.append(Finding(f'{owner} inputs.{key}', Kind.DECLARED_INPUT, input_to_mark=key))
            if key in outputs and _clear(outputs[key]):
                found.append(Finding(f'{owner} outputs.{key}', Kind.DECLARED_OUTPUT))
        found.extend(_carried(inputs, outputs, f'{owner} outputs', None))
        found.extend(
            Finding(where, Kind.PLAINTEXT)
            for where, value in _envelopes(resource, owner, '')
            if 'plaintext' in cast('dict[str, object]', value)
        )
        for finding in found:
            _ = findings.setdefault(finding.where, finding)
    return list(findings.values())


def recorded(document: Mapping[str, Any]) -> Iterator[tuple[str, dict[str, Any]]]:
    """Every resource state a checkpoint file records, with the name a finding gives it.

    Each of `latest.resources`, by its URN, and the resource of each of
    `latest.pending_operations`, by its place in that list and its URN: the
    state the engine began an operation from, which the file holds from
    before the operation runs until it ends, and for good where a run was
    killed partway.
    """
    for resource in resources(document):
        yield str(resource.get('urn', '?')), resource
    operations = cast('list[object]', _member(document, 'checkpoint', 'latest', 'pending_operations') or [])
    for index, operation in enumerate(operations):
        resource = _member(cast('dict[str, Any]', operation), 'resource') if isinstance(operation, dict) else None
        if isinstance(resource, dict):
            resource = cast('dict[str, Any]', resource)
            yield f'pending_operations[{index}] {resource.get("urn", "?")}', resource


def _carried(
    inputs: Mapping[str, object], outputs: Mapping[str, object], where: str, top: str | None
) -> Iterator[Finding]:
    """The engine's `annotateSecrets`, read against the file rather than applied to it.

    For each input with an output of the same name: where both are objects,
    the same again one level down; otherwise, where the input holds
    ciphertext anywhere — an array one element of which is secret, an
    object one member of which is — the whole output is ciphertext. `top`
    is the resource's input the walk is under, which the finding names as
    its input to mark: the engine wraps an output whole for an input of its
    name that is wholly secret, at the top of the resource, whichever
    provider wrote it.
    """
    for key, given in inputs.items():
        if key not in outputs:
            continue
        held = outputs[key]
        if _object(given) and _object(held):
            yield from _carried(
                cast('dict[str, object]', given), cast('dict[str, object]', held), f'{where}.{key}', top or key
            )
        elif _clear(held) and next(_envelopes(given, '', ''), None) is not None:
            yield Finding(f'{where}.{key}', Kind.CARRIED, input_to_mark=top or key)


def _object(value: object) -> bool:
    """Whether the engine reads `value` as an object: a map, and not one carrying the signature key.

    A signed map is a secret, an asset, an archive or a resource reference,
    each a value of its own kind rather than an object to recurse into.
    """
    return isinstance(value, dict) and SECRET_SIG not in cast('dict[str, object]', value)


def _clear(value: object) -> bool:
    """Whether `value`, where the engine marks it secret, is anything but an envelope or a null."""
    return value is not None and not envelope(value)


def record(checkout: Path, stack: str) -> Path:
    """Where a failed or unfinished check of `stack`'s checkpoint is recorded until a run's checks pass.

    In `checkpoints/` rather than beside the checkpoint, so it is found by the
    stack's name alone, before a first `stack init` has made a project
    directory to hold one.
    """
    return checkout / stack_environment.CHECKPOINTS / f'{stack}.failed-check'


#: The files of a stack the backend's `stacks/<project>/` directory may hold:
#: the checkpoint, and the copy of the one before it that every save makes.
EXPECTED = ('{stack}.json', '{stack}.json.bak')


def strays(checkout: Path, stack: str) -> list[Path]:
    """Every file for `stack` under the backend's `stacks/` other than its checkpoint and that one's `.bak`.

    A file named for the stack is a copy of its state in some form the checks
    do not read — compressed (`.json.gz`, `.json.zst`), retained with a
    timestamp, or in another layout — and so one that could publish what the
    checks would have refused.
    """
    stacks = checkout / stack_environment.CHECKPOINTS / '.pulumi' / 'stacks'
    expected = {name.format(stack=stack) for name in EXPECTED}
    return sorted(
        path
        for path in stacks.rglob('*')
        if path.is_file()
        and (path.name == stack or path.name.startswith(f'{stack}.'))
        and not (path.parent.parent == stacks and path.name in expected)
    )


def _texts(document: Mapping[str, Any]) -> Iterator[tuple[str, str]]:
    """Every string in a checkpoint file outside an envelope, keys included, with where it is."""
    rest = copy.deepcopy(dict(document))
    if isinstance(latest := _member(rest, 'checkpoint', 'latest'), dict):
        _ = cast('dict[str, Any]', latest).pop('resources', None)
    yield from _walk(rest, 'the checkpoint', '')
    for resource in resources(document):
        yield from _walk(resource, str(resource.get('urn', '?')), '')


def _walk(value: object, owner: str, path: str) -> Iterator[tuple[str, str]]:
    """The strings under `value`, each named by its owner and its path in it."""
    if envelope(value):
        return
    if isinstance(value, str):
        yield f'{owner} {path}'.rstrip(), value
    elif isinstance(value, dict):
        for key, item in cast('dict[str, object]', value).items():
            inner = f'{path}.{key}' if path else key
            yield f'{owner} {inner}', key
            yield from _walk(item, owner, inner)
    elif isinstance(value, list):
        for index, item in enumerate(cast('list[object]', value)):
            yield from _walk(item, owner, f'{path}[{index}]')


def _envelopes(value: object, owner: str, path: str) -> Iterator[tuple[str, object]]:
    """The envelopes under `value`, each named by its owner and its path in it."""
    if envelope(value):
        yield f'{owner} {path}'.rstrip(), value
    elif isinstance(value, dict):
        for key, item in cast('dict[str, object]', value).items():
            yield from _envelopes(item, owner, f'{path}.{key}' if path else key)
    elif isinstance(value, list):
        for index, item in enumerate(cast('list[object]', value)):
            yield from _envelopes(item, owner, f'{path}[{index}]')


def _strings(value: object) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for item in cast('dict[str, object]', value).values():
            yield from _strings(item)
    elif isinstance(value, list):
        for item in cast('list[object]', value):
            yield from _strings(item)
