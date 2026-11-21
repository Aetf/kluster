"""`checks-scope`: whether `checks` runs in full, or only the cases that read the committed stack files.

    mise x uv -- uv run checks-scope --event pull_request --base HEAD^1 --github-output "$GITHUB_OUTPUT"

It compares the commit checked out (`HEAD`) with `--base`, prints one line
naming its verdict and the reason for it, and writes the verdict as the step
output `scope` when `--github-output` names the file. The verdict is
`STACK_FILES` only when every one of these holds, and `FULL` otherwise:

1.  the event is a pull request, and the base is a commit;
2.  the two commits differ, and every path that differs is modified in place,
    a regular file at both, with no add, delete, rename or mode change; and
    each such path is a stack file (`Pulumi.<stack>.yaml` at the root, the
    project file `Pulumi.yaml` never one) or the etcd restore-verify's
    recipient file (`conventions.backup.ETCD_DRILL_RECIPIENT`);
3.  each stack file parses as a mapping at both commits, and the two are
    the same document once the project's sealed key
    (`conventions.sealed.CONFIG_KEY`) and kubeconfig key
    (`credentials`'s `slots.KUBECONFIG_KEY`) are taken out of each one's
    `config`. The project is the `name` of `Pulumi.yaml` at `HEAD`. Two
    documents are the same when their node trees are (`_tree`): each scalar
    is its tag and its text, so layout, comments, quoting and key order
    are not a change, while `1` and `true`, `1` and `1.0`, or `017` and `17`
    are, whatever value a loader would construct from them;
4.  at `HEAD`, each stack file's kubeconfig key is absent or a `secure:`
    ciphertext and nothing else, so a copy in the clear is `FULL`; and the
    recipient file passes the reader the stack program reads it with
    (`kluster.lib.etcd_backup.drill_recipient`).

This is the one statement of the shape besides framework/ci.md §3, which
every other mention points at. The verdict decides what runs and judges
nothing: what the ciphertexts and the copies hold is for the cases
`-m stack_files` selects, the cases that read a committed stack file
(docs/framework/testing.md §2), and a push to `main` runs everything.
Anything else, an error included, is `FULL`: a git command that fails or a
file that does not parse is `FULL` with that as the reason, and an error
nothing here foresees is `FULL` with the error as the reason and its
traceback logged, so it never fails the step.
"""

from __future__ import annotations

import argparse
import logging
import re
import subprocess
import sys
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, cast

from ruamel.yaml import YAML, YAMLError
from ruamel.yaml.nodes import MappingNode, Node, ScalarNode, SequenceNode

from kluster.conventions import sealed
from kluster.conventions.backup import ETCD_DRILL_RECIPIENT
from kluster.lib import etcd_backup, workstation
from kluster.lib.state_backend import committed

from .credentials import slots

log = logging.getLogger(__name__)

#: The two verdicts, as the step output `scope` carries them.
STACK_FILES = 'stack-files'
FULL = 'full'

#: The one event that can take the fast path.
PULL_REQUEST = 'pull_request'

#: A stack file: `Pulumi.<stack>.yaml` at the root of the checkout, `<stack>` non-empty.
STACK_FILE = re.compile(r'Pulumi\.[^/]+\.yaml')
#: The project file, which names the project the configuration keys are in.
PROJECT_FILE = 'Pulumi.yaml'
#: The recipient file, as a path in the checkout.
RECIPIENT_FILE = PurePosixPath(ETCD_DRILL_RECIPIENT.relative_to(workstation.repo_root(ETCD_DRILL_RECIPIENT)))

#: The tag a plain or quoted string resolves to, a configuration key's.
STRING_TAG = 'tag:yaml.org,2002:str'

#: Git's status for a path modified in place, and its mode for a regular file
#: that is not executable, which is what every admitted path is.
MODIFIED = 'M'
REGULAR = '100644'

#: Each git command's bound, in seconds. A run reads a handful of small blobs.
TIMEOUT = 60


@dataclass(frozen=True)
class Verdict:
    scope: str
    reason: str


class Undecided(Exception):
    """Something the verdict reads could not be read: a git command failed, or a file does not parse."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='checks-scope',
        description=(
            f'Decide whether `checks` runs in full ({FULL}) or only the cases that read the committed stack files '
            f'({STACK_FILES}), the fast path of docs/framework/ci.md §3.'
        ),
    )
    _ = parser.add_argument('--event', required=True, help='the event the run is for, `github.event_name`')
    _ = parser.add_argument('--base', required=True, help='the commit HEAD is compared with')
    _ = parser.add_argument('--github-output', type=Path, help='the file the step output `scope` is appended to')
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    args = build_parser().parse_args(argv)
    event: str = args.event
    base: str = args.base
    output: Path | None = args.github_output
    found = verdict(Path.cwd(), event=event, base=base)
    print(f'scope: {found.scope} ({found.reason})')
    if output is not None:
        with output.open('a') as file:
            _ = file.write(f'scope={found.scope}\n')
    return 0


def verdict(checkout: Path, *, event: str, base: str) -> Verdict:
    """The scope a `checks` run in `checkout` takes, for `event`, comparing `HEAD` with `base`."""
    if event != PULL_REQUEST:
        return Verdict(FULL, f'the event is {event or "unnamed"}, and only a pull request takes the fast path')
    try:
        return _compared(checkout, base)
    except Undecided as exc:
        return Verdict(FULL, str(exc))
    # Anything else is `FULL` too, rather than a failed step: an error here
    # costs a full run, never the fast path, and never a red job.
    except Exception as exc:
        log.exception('checks-scope failed, so the run is in full')
        return Verdict(FULL, f'checks-scope failed: {exc!r}')


def _compared(checkout: Path, base: str) -> Verdict:
    if not base or not _is_commit(checkout, base):
        return Verdict(FULL, f'the base {base or "(none)"} is not a commit here')
    changes = _changes(checkout, base)
    if not changes:
        return Verdict(FULL, f'HEAD changes nothing against {base}')
    for path, change in changes.items():
        if change != (MODIFIED, REGULAR, REGULAR):
            return Verdict(FULL, f'{path} is not a file modified in place ({" ".join(change)})')
        if not (STACK_FILE.fullmatch(path) or PurePosixPath(path) == RECIPIENT_FILE):
            return Verdict(FULL, f'{path} is neither a stack file nor {RECIPIENT_FILE}')
    project = _document(checkout, 'HEAD', PROJECT_FILE).get('name')
    if not isinstance(project, str):
        return Verdict(FULL, f'{PROJECT_FILE} at HEAD names no project')
    for path in changes:
        if PurePosixPath(path) == RECIPIENT_FILE:
            refusal = _recipient_refusal(checkout, path)
            if refusal is not None:
                return Verdict(FULL, refusal)
            continue
        refusal = _stack_file_refusal(checkout, base, path, project)
        if refusal is not None:
            return Verdict(FULL, refusal)
    return Verdict(
        STACK_FILES, f'every change is a sealed value, a kubeconfig copy or the recipient: {", ".join(changes)}'
    )


def _changes(checkout: Path, base: str) -> dict[str, tuple[str, str, str]]:
    """Every path that differs between `base` and `HEAD`: its status and its mode at each.

    `--raw` rather than `--name-status`, because a mode change alone is
    status `M` there, and `-z` so a name reads as it is.
    """
    fields = _git(checkout, 'diff', '--raw', '--no-renames', '-z', base, 'HEAD').split('\0')
    changes: dict[str, tuple[str, str, str]] = {}
    for header, path in zip(fields[0:-1:2], fields[1::2], strict=True):
        old_mode, new_mode, _old, _new, status = header.removeprefix(':').split(' ')
        changes[path] = (status, old_mode, new_mode)
    return changes


def _stack_file_refusal(checkout: Path, base: str, path: str, project: str) -> str | None:
    """Why `path`'s change is not the fast path's shape, or `None` when it is."""
    keys = (f'{project}:{sealed.CONFIG_KEY}', f'{project}:{slots.KUBECONFIG_KEY}')
    _, before = _parsed(checkout, base, path)
    after, after_node = _parsed(checkout, 'HEAD', path)
    if _tree(_without(before, keys)) != _tree(_without(after_node, keys)):
        return f'{path} changes something besides {" and ".join(keys)}'
    copy = _config(after, path).get(keys[1])
    if copy is not None and not _ciphertext(copy):
        return f'{path} holds {keys[1]} as something other than a `secure:` ciphertext'
    return None


def _ciphertext(value: object) -> bool:
    """Whether `value` is how a stack file holds a secret: a mapping of `secure:` to a string and nothing else."""
    if not isinstance(value, Mapping):
        return False
    held = cast('Mapping[object, object]', value)
    return set(held) == {'secure'} and isinstance(held['secure'], str)


def _recipient_refusal(checkout: Path, path: str) -> str | None:
    """Why the recipient file at `HEAD` is not one the stack program reads, or `None` when it is."""
    text = _git(checkout, 'show', f'HEAD:{path}')
    with tempfile.TemporaryDirectory(prefix='checks-scope-') as directory:
        copy = Path(directory) / RECIPIENT_FILE.name
        _ = copy.write_text(text)
        try:
            _ = etcd_backup.drill_recipient(copy)
        except committed.Refused as exc:
            return f'{path} at HEAD is not a recipient the stack reads: {exc}'
    return None


def _parsed(checkout: Path, revision: str, path: str) -> tuple[dict[str, object], MappingNode]:
    """`path` at `revision`, loaded and as a node tree.

    Loaded as well as composed, so a document the loader refuses -- a
    duplicate key, a tag with no constructor -- is refused here too.
    """
    text = _git(checkout, 'show', f'{revision}:{path}')
    document = _loaded(text, revision, path)
    # ruamel.yaml leaves `compose`'s stream parameter unannotated.
    node: object = YAML(typ='safe').compose(text)  # pyright: ignore[reportUnknownMemberType]
    assert isinstance(node, MappingNode), 'a document that loads as a mapping composes as one'
    return document, node


def _without(document: MappingNode, keys: Sequence[str]) -> MappingNode:
    """`document` with `keys` taken out of its `config`, and a `config` left empty taken out too."""
    pairs: list[tuple[Node, Node]] = []
    for key, value in _held(document):
        if not (_is_string(key, 'config') and isinstance(value, MappingNode)):
            pairs.append((key, value))
            continue
        kept = [(inner, held) for inner, held in _held(value) if not any(_is_string(inner, name) for name in keys)]
        if kept:
            pairs.append((key, MappingNode(_tag(value), kept)))
    return MappingNode(_tag(document), pairs)


def _is_string(node: Node, text: str) -> bool:
    return isinstance(node, ScalarNode) and _tag(node) == STRING_TAG and _held(node) == text


def _tree(node: Node) -> object:
    """`node` as a value two documents compare by: each scalar its tag and its text, a mapping unordered."""
    if isinstance(node, ScalarNode):
        return ('scalar', _tag(node), str(_held(node)))
    if isinstance(node, SequenceNode):
        items: list[Node] = _held(node)
        return ('sequence', _tag(node), tuple(_tree(item) for item in items))
    if isinstance(node, MappingNode):
        pairs: list[tuple[Node, Node]] = _held(node)
        return ('mapping', _tag(node), frozenset((_tree(key), _tree(value)) for key, value in pairs))
    raise Undecided(f'a {type(node).__name__} is not a node this reads')


def _tag(node: Node) -> str:
    """A node's tag, as text; ruamel.yaml leaves its node attributes unannotated."""
    return str(cast('Any', node).tag)


def _held(node: Node) -> Any:
    """A node's value: a scalar's text, a sequence's nodes, or a mapping's pairs of nodes."""
    return cast('Any', node).value


def _config(document: Mapping[str, object], path: str) -> Mapping[str, object]:
    config = document.get('config')
    if config is None:
        return {}
    if not isinstance(config, Mapping):
        raise Undecided(f'the config of {path} is not a mapping')
    return cast('Mapping[str, object]', config)


def _document(checkout: Path, revision: str, path: str) -> dict[str, object]:
    return _loaded(_git(checkout, 'show', f'{revision}:{path}'), revision, path)


def _loaded(text: str, revision: str, path: str) -> dict[str, object]:
    try:
        # ruamel.yaml leaves `load`'s stream parameter unannotated.
        document: object = YAML(typ='safe').load(text)  # pyright: ignore[reportUnknownMemberType]
    except YAMLError as exc:
        raise Undecided(f'{path} at {revision} does not parse: {exc}') from None
    if not isinstance(document, Mapping):
        raise Undecided(f'{path} at {revision} is not a mapping')
    return cast('dict[str, object]', document)


def _is_commit(checkout: Path, revision: str) -> bool:
    try:
        _ = _git(checkout, 'cat-file', '-t', f'{revision}^{{commit}}')
    except Undecided:
        return False
    return True


def _git(checkout: Path, *arguments: str) -> str:
    """What `git <arguments>` prints in `checkout`, refused as `Undecided` when it fails."""
    command = ['git', '--no-pager', *arguments]
    try:
        done = subprocess.run(command, cwd=checkout, capture_output=True, text=True, timeout=TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Undecided(f'`git {" ".join(arguments)}` did not run: {exc}') from None
    if done.returncode != 0:
        raise Undecided(f'`git {" ".join(arguments)}` failed: {done.stderr.strip() or done.returncode}')
    return done.stdout


if __name__ == '__main__':
    sys.exit(main())
