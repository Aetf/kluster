"""The `change-detector` gate: its extractor over real diffs, and the gate run end to end against recorded replies.

Each diff is recorded under `tests/data/gates/change_detector/<name>/`: the
files it starts from under `base/` (each with `.txt` after its path), and the
change as `git diff` printed it.

-   `devices`: Aetf/kluster#486's to `tests/test_devices.py`, at the head its
    review read. It rewrites assertions, writes `pytest.raises` both with and
    without a `match`, and leaves most of the module's assertions alone.
-   `bucket`: Aetf/kluster#472's to `tests/test_backup_bucket.py`, at the head
    its review read, beside the `conventions` package and the `backup`
    component it reads. It rewrites the inner lines of a multi-line assertion
    and leaves its first line alone, and the case reads `conventions`
    attributes that the package re-exports from a submodule, and some of the
    names the module imports from the component.
-   `session`: the commit that added `tests/test_unifi_session.py` and the
    module it tests, which the test imports under another name.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any, cast

import pytest
from gate_stub import PULL, GateMemoryForge, Recorded, commit, git, pull_request, scratch_repository

from kluster.lib import workstation
from kluster.scripts.dispatch.gates import change_detector, run
from kluster.scripts.dispatch.gates.calibration import Status
from kluster.scripts.dispatch.gates.extract import Item, change
from kluster.scripts.dispatch.gates.gate import RECORD, Gate
from kluster.scripts.dispatch.gates.verdict import SAMPLES
from kluster.scripts.dispatch.prcheck import Git

DATA = Path(__file__).parent / 'data' / 'gates' / 'change_detector'
CANON = change_detector.GATE.canon.document
DEVICES = 'tests/test_devices.py'
SESSION = 'tests/test_unifi_session.py'


@pytest.fixture
def repository(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    return scratch_repository(tmp_path, monkeypatch)


def _diff(repository: Path, name: str) -> tuple[str, str]:
    """The base and the head of the recorded diff `name`, committed in `repository` beside the canon as this tree holds it."""
    recorded = DATA / name
    files = {CANON: (workstation.repo_root() / CANON).read_text()}
    for path in sorted((recorded / 'base').rglob('*.txt')):
        files[str(path.relative_to(recorded / 'base').with_suffix(''))] = path.read_text()
    base = commit(repository, files)
    _ = git(repository, 'apply', str(recorded / 'change.patch'))
    head = commit(repository, {})
    return base, head


def _items(repository: Path, name: str) -> list[Item]:
    base, head = _diff(repository, name)
    # A recorded diff has no pull request around it, so no description.
    return list(change_detector.GATE.extract(change(Git(repository), base, head, body='')))


def test_the_extractor_draws_each_assertion_the_diff_writes_and_no_other(repository: Path) -> None:
    items = _items(repository, 'devices')

    # The written and rewritten asserts, and the `pytest.raises` whose `match`
    # the diff rewrites (283). The two it writes without a `match` (362, 415)
    # assert no wording, and the asserts it leaves alone are not its own.
    assert [(i.path, i.line, i.end_line) for i in items] == [
        (DEVICES, line, line) for line in (199, 200, 283, 286, 365, 418)
    ]


def test_an_assertion_the_diff_rewrites_only_inside_is_drawn_whole(repository: Path) -> None:
    items = _items(repository, 'bucket')

    # The diff rewrites 120–122 of the assertion at 119–123, and not 119.
    (rewritten,) = [i for i in items if i.line == 119]
    assert (rewritten.end_line, rewritten.text.split('\n')[0]) == (123, '    assert prefixes == {')


def test_an_item_carries_its_case_whole_and_the_module_level_names_the_case_reads(repository: Path) -> None:
    items = {i.line: i for i in _items(repository, 'devices')}
    lines = (repository / DEVICES).read_text().split('\n')

    # The case runs from its `def` at 394 to its last assertion at 426.
    case = '\n'.join(lines[393:426])
    assert items[418].context.endswith(f'# {DEVICES}:394–426, the enclosing case\n{case}')
    # The case at 191 reads the row `ADGUARD`, given whole; the helper `stack`,
    # by its signature; and the fixture `typed` it takes as a parameter, by
    # its signature and docstring. The bodies of the two functions are not
    # given, nor is the helper `parsed_commands`, which this case never reads.
    context = items[199].context
    assert f'# {DEVICES}:28\nADGUARD = devices.DEVICES' in context
    assert f'# {DEVICES}:51\ndef stack(name: str)' in context
    assert 'runner = RecordedPulumi()' not in context.partition('the enclosing case')[0]
    assert f'# {DEVICES}:64–71\n@pytest.fixture\ndef typed(' in context
    assert 'monkeypatch.setattr' not in context
    assert 'parsed_commands' not in context


def test_an_item_carries_the_programs_definition_of_each_name_the_case_reads(repository: Path) -> None:
    items = {i.line: i for i in _items(repository, 'bucket')}

    # `conventions` re-exports the three from `conventions.backup`, where they
    # are defined: the prefix typed at its declaration, the two paths computed.
    # The package's own text, its re-exports, is not what decides.
    context = items[119].context
    backup = 'src/kluster/conventions/backup.py'
    assert f"# {backup}:105\nETCD_SNAPSHOT_PREFIX = 'etcd'" in context
    assert f'# {backup}:97\ndef volsync_repo_path(namespace: str, pvc: str) -> str:' in context
    assert f'# {backup}:101\ndef barman_repo_path(' in context
    assert 'src/kluster/conventions/__init__.py' not in context
    # Of the names the module imports from the component, the case reads the
    # scopes and not `s3_endpoint`.
    component = 'src/kluster/components/backup/__init__.py'
    assert f'# {component}:95–101\ndef etcd_scope() -> Scope:\n    """The control plane' in context
    assert 'def s3_endpoint' not in context


def test_a_module_imported_under_another_name_is_read_by_that_name(repository: Path) -> None:
    items = {i.line: i for i in _items(repository, 'session')}

    # The case reads `session.API_KEY_CONFIG`, where `session` is
    # `kluster.providers.unifi_session` imported as `session`.
    context = items[38].context
    assert f'# {SESSION}:16\nfrom kluster.providers import unifi_session as session' in context
    assert "# src/kluster/providers/unifi_session.py:61\nAPI_KEY_CONFIG = 'unifiApiKey'" in context


def _replies() -> Recorded:
    return Recorded([cast(dict[str, Any], json.loads(p.read_text())) for p in sorted((DATA / 'messages').iterdir())])


def test_the_gate_runs_in_shadow_end_to_end_against_recorded_replies(tmp_path: Path, repository: Path) -> None:
    base, head = _diff(repository, 'devices')
    forge = GateMemoryForge()
    pull_request(forge, base, head)
    # The gate as this tree holds it, less any record a calibration wrote:
    # a builder's gate has none, and runs in shadow until it is measured.
    copy = tmp_path / 'change_detector'
    _ = shutil.copytree(change_detector.__path__[0], copy, ignore=shutil.ignore_patterns(RECORD, '__pycache__'))
    gate = Gate.load(copy, change_detector.GATE)
    replies = _replies()

    posted = run.review(forge, Git(repository), PULL, [gate], replies.model)

    # One batch, each sample asked with the canon as the head holds it.
    assert len(replies.requests) == SAMPLES
    section = gate.canon(lambda path: (repository / path).read_text()).text
    assert all(section in request['system'] for request in replies.requests)
    assert posted is not None
    (ran,) = posted.evidence.gates
    assert (ran.gate, ran.status) == ('change-detector', Status.SHADOW)
    assert [e.line for e in ran.findings] == [283, 418]
    assert not run.refuses(0, posted)
