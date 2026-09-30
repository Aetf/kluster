"""What a run of a stack whose state is committed is checked for (framework/pulumi.md §3.3).

The checkpoint is a tracked file of this repository, and **the publication is
the push**: a branch carrying it is public from the moment it reaches the
forge, before any review. So the checks here run on the workstation, where the
file is still private:

-   **Before a run**, that the working copy holds the forge's current `main`
    (`require_current`), and that the checkpoint is not conflicted. A run from
    an older checkpoint plans against a state that is not the latest one.
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
import json
import subprocess as sp
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from kluster.lib import stack_environment

#: The two keys of Pulumi's secret envelope: the signature key, and the value
#: it holds in every envelope. A file carries the value encrypted beside them
#: as `ciphertext`; `pulumi stack export --show-secrets` carries it decrypted,
#: JSON-encoded, as `plaintext`.
SECRET_SIG = '4dabf18193072939515e22adb298388d'
SECRET_MARK = '1b47061264138c4ac30d75fd1eb44270'

#: Where the forge's `main` is read from, as `git ls-remote` names it.
REMOTE = 'origin'
MAIN = 'refs/heads/main'

#: The shortest string searched for as a secret. A value this short carries too
#: little to be told apart from ordinary text in the file, so searching for it
#: would find it everywhere; the secrets this repository keeps are keys and
#: tokens, many times longer.
SHORTEST = 8

#: How long one `git` or `jj` query may take. `ls-remote` talks to the forge.
TIMEOUT = 60


class CheckRefused(RuntimeError):
    """The working copy is not one a run of this stack may start from."""


@dataclass(frozen=True)
class Finding:
    """A property of the checkpoint that must not be published as it stands."""

    where: str
    what: str

    def __str__(self) -> str:
        return f'{self.where}: {self.what}'


# --------------------------------------------------------------------------
# Before a run: the working copy.
# --------------------------------------------------------------------------


def require_current(checkout: Path, checkpoint: Path | None) -> None:
    """Refuse a run from a working copy behind the forge's `main`, or over a conflicted checkpoint.

    **The ancestry comes first**, because of what the conflict's remedy does.
    After a checkpoint lands, the fetch that abandons the landed change can
    leave a second, unlanded run's checkpoint conflicted in the working copy
    until the rebase resolves it; checked first, the conflict's remedy would
    throw that unlanded run away, where the ancestry's refusal sends the
    operator to the rebase that resolves it.
    """
    _require_ancestry(checkout)
    if checkpoint is not None and conflicted(checkpoint):
        raise CheckRefused(
            f"{checkpoint} is conflicted: two runs wrote it from one base. Keep main's side "
            f'(`jj restore --from main {checkpoint.relative_to(checkout)}`), run a refreshed `plan`, and bring in '
            "through the program's `import_` what the other run created, or delete it by hand. An instance "
            'the other run created is terminated by hand, never imported.'
        )


def _require_ancestry(checkout: Path) -> None:
    """The forge's `main`, read without writing a ref, must be an ancestor of the working copy.

    `git merge-base --is-ancestor` answers 0 when it is, 1 when it is not, and
    128 when the commit is not in this repository at all — the forge's `main`
    never fetched. Every answer but 0 is a refusal, and the remedy is the
    step that is missing.
    """
    listed = _run(['git', 'ls-remote', REMOTE, MAIN], checkout, what="reading the forge's main")
    fields = listed.split()
    if not fields:
        raise CheckRefused(f'`git ls-remote {REMOTE} {MAIN}` names no commit, so there is no main to start from')
    forge_main = fields[0]
    head = working_copy(checkout)
    answer = sp.run(
        ['git', 'merge-base', '--is-ancestor', forge_main, head],
        cwd=checkout,
        capture_output=True,
        text=True,
        timeout=TIMEOUT,
        check=False,
    )
    if answer.returncode == 1:
        raise CheckRefused(
            f"the forge's main ({forge_main[:12]}) is fetched but the working copy is not on it: "
            '`jj rebase -b @ -d main`, then run again'
        )
    if answer.returncode != 0:
        raise CheckRefused(
            f"the forge's main ({forge_main[:12]}) is not in this repository: `jj git fetch`, "
            'then `jj rebase -b @ -d main`, then run again'
        )


def working_copy(checkout: Path) -> str:
    """The commit the working copy is: `jj`'s `@` in a `jj` checkout, git's `HEAD` in a plain clone.

    `jj` snapshots the working copy first, as every `jj` command does, and
    refuses one that is stale — one whose `@` another workspace rewrote, by a
    rebase or by a fetch that abandoned what `@` sat on. The files on disk are
    then not `@`'s, and they are what the conflict check and `pulumi` read, so
    a stale working copy is refused here rather than read past.
    """
    if (checkout / '.jj').is_dir():
        command = ['jj', 'log', '--no-graph', '-r', '@', '-T', 'commit_id']
    else:
        command = ['git', 'rev-parse', 'HEAD']
    return _run(command, checkout, what='reading the working copy').strip()


def conflicted(checkpoint: Path) -> bool:
    """Whether `checkpoint` holds conflict markers.

    Read from the file, so the answer is the same whichever tool left them: a
    `jj` conflict and a `git` merge both write lines opening with seven or
    more `<`, and the checkpoint Pulumi writes is one line of JSON that opens
    with `{`.
    """
    return any(line.startswith('<<<<<<<') for line in checkpoint.read_text().splitlines())


def _run(command: Sequence[str], cwd: Path, *, what: str) -> str:
    completed = sp.run(list(command), cwd=cwd, capture_output=True, text=True, timeout=TIMEOUT, check=False)
    if completed.returncode != 0:
        raise CheckRefused(f'{what} failed: `{" ".join(command)}`: {completed.stderr.strip() or completed.returncode}')
    return completed.stdout


# --------------------------------------------------------------------------
# Around a run: an unchanged deployment keeps its bytes.
# --------------------------------------------------------------------------


def same_deployment(before: Mapping[str, Any], after: Mapping[str, Any]) -> bool:
    """Whether two `pulumi stack export --show-secrets` documents hold the same deployment.

    Compared without the manifest's timestamp, which every write moves, and
    with the resources in one canonical order, since the engine does not keep
    theirs between runs when resources register concurrently. The export carries each
    secret's envelope beside its plaintext rather than a ciphertext, so a
    re-encryption under a fresh nonce is no difference, and a property that
    became secret is one.
    """
    return _normal(before) == _normal(after)


def _normal(export: Mapping[str, Any]) -> object:
    document = copy.deepcopy(dict(export))
    deployment = cast('dict[str, Any]', document.get('deployment') or {})
    manifest = cast('dict[str, Any]', deployment.get('manifest') or {})
    _ = manifest.pop('time', None)
    resources = cast('list[Any]', deployment.get('resources') or [])
    deployment['resources'] = sorted(resources, key=lambda resource: json.dumps(resource, sort_keys=True))
    return document


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
    return [Finding(where, f'holds the value of {", ".join(found)} in the clear') for where, found in sources.items()]


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
                found.append(
                    Finding(
                        f'{owner} inputs.{key}',
                        f'is named in additionalSecretOutputs and is not ciphertext; {_REMEDY.format(key=key)}',
                    )
                )
            if key in outputs and _clear(outputs[key]):
                found.append(
                    Finding(f'{owner} outputs.{key}', 'is named in additionalSecretOutputs and is not ciphertext')
                )
        found.extend(_carried(inputs, outputs, f'{owner} outputs', None))
        found.extend(
            Finding(where, 'is a secret envelope holding its plaintext')
            for where, value in _envelopes(resource, owner, '')
            if 'plaintext' in cast('dict[str, object]', value)
        )
        for finding in found:
            _ = findings.setdefault(finding.where, finding)
    return list(findings.values())


#: How an input refusal is cleared: the input passed whole as a secret is one
#: envelope at the next write, and the engine then wraps the output of that
#: name too (framework/pulumi.md §3.3).
_REMEDY = 'pass the input `{key}` whole as `pulumi.Output.secret(...)`'


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
    is the resource's input the walk is under, which the remedy names: the
    engine wraps an output whole for an input of its name that is wholly
    secret, at the top of the resource, whichever provider wrote it.
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
            yield Finding(
                f'{where}.{key}',
                'holds ciphertext as an input and is not ciphertext as an output; ' + _REMEDY.format(key=top or key),
            )


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
