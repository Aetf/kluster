"""`typecheck` runs the check under its heap, fails a run that emptied its type cache, and otherwise exits as the check did.

The exits are read against a stand-in: a Python process printing what
basedpyright prints. The rest is read on the real check, over a generated
tree that outgrows a small heap in a few seconds, as a full run of this tree
outgrows Node's default one: `main` run on it exits `EVICTED`, and the pinned
basedpyright run on it under that heap still drops the report of a stale
ignore. That second case is the tripwire of the layer `typecheck` is: when
the defect is fixed upstream, it fails and names the layer to remove.

Node counts its young generation, 192 MiB by default, into the limit
basedpyright measures against, so with a small old space it runs out before
basedpyright's threshold of 0.857 of the limit is reached. The tree's runs
take that share out with `--max-semi-space-size=1`.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import process_sessions
import pytest

from kluster.scripts import typecheck


def checker(*lines: str, status: int = 0) -> list[str]:
    """A command that prints `lines` and exits with `status`, as basedpyright would."""
    return [sys.executable, '-c', f'print(*{lines!r}, sep="\\n"); raise SystemExit({status})']


EVICTION_LINE = f'{typecheck.EVICTION}. Used 3738MB out of 4288MB.'

#: An old space the generated tree outgrows.
SMALL_HEAP_MB = 120
#: Takes the young generation's share out of the heap limit (module docstring).
SMALL_YOUNG_GENERATION = '--max-semi-space-size=1'
#: One run over the generated tree takes about three seconds.
STOP_LOSS = 45

#: The stale ignore the removal case plants, and the report basedpyright makes of it.
STALE_IGNORE = 'X: int = 1  # pyright: ignore[reportAttributeAccessIssue]\n'
UNNECESSARY = 'Unnecessary "# pyright: ignore"'


def outgrown_tree(root: Path, *, stale_ignore: bool) -> Path:
    """A project of 81 files that outgrows `SMALL_HEAP_MB`; with `stale_ignore`, `f0.py` carries one.

    Every other file imports `f0`, so a file checked before an eviction is
    parsed again after it, which is the file whose report goes missing.
    """
    ignore_rule = ', "reportUnnecessaryTypeIgnoreComment": "error"' if stale_ignore else ''
    _ = (root / 'pyrightconfig.json').write_text(f'{{"typeCheckingMode": "standard"{ignore_rule}}}')
    _ = (root / 'f0.py').write_text(STALE_IGNORE if stale_ignore else 'X: int = 1\n')
    cls = 'class C{j}:\n    def m(self, a: int, b: str) -> dict[str, list[int]]:\n        return {{b: [a, {j}]}}\n'
    body = ''.join(cls.format(j=j) + f'v{j} = C{j}().m({j}, "x")\n' for j in range(40))
    for i in range(1, 81):
        _ = (root / f'f{i}.py').write_text(f'from f0 import X\n\n{body}Y = X\n')
    return root


@pytest.mark.parametrize('status', [0, 1, 3])
def test_a_run_that_kept_its_cache_exits_as_the_check_did(status: int, capsys: pytest.CaptureFixture[str]) -> None:
    lines = ('/repo/src/a.py', '  /repo/src/a.py:3:1 - error: oops', '1 error, 0 warnings, 0 notes')
    assert typecheck.check(checker(*lines, status=status), os.environ) == status
    assert capsys.readouterr().out.splitlines() == list(lines)


def test_a_clean_run_that_emptied_its_cache_fails(
    capsys: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
) -> None:
    lines = ('Found 376 source files', EVICTION_LINE, EVICTION_LINE, '0 errors, 0 warnings, 0 notes')
    assert typecheck.check(checker(*lines), os.environ) == typecheck.EVICTED
    assert capsys.readouterr().out.splitlines() == list(lines)
    assert 'emptied its type cache 2 times,' in caplog.text


def test_a_failed_run_that_emptied_its_cache_keeps_the_checks_status(caplog: pytest.LogCaptureFixture) -> None:
    assert typecheck.check(checker(EVICTION_LINE, '1 error, 0 warnings, 0 notes', status=1), os.environ) == 1
    assert 'emptied its type cache once,' in caplog.text


@pytest.mark.parametrize('caller', [SMALL_YOUNG_GENERATION, f'{SMALL_YOUNG_GENERATION} --max-old-space-size=4096'])
def test_main_fails_a_tree_that_outgrows_its_heap(tmp_path: Path, caller: str) -> None:
    """`main`, on a tree too large for the heap it hands basedpyright, exits `EVICTED` and keeps the caller's options.

    The tree is one the check passes, so only the eviction can fail it. A
    larger limit the caller already names gives way to `HEAP_MB`.
    """
    tree = outgrown_tree(tmp_path, stale_ignore=False)
    program = (
        'import sys; from kluster.scripts import typecheck as t; '
        f't.HEAP_MB = {SMALL_HEAP_MB}; t.COMMAND = (*t.COMMAND, "-p", sys.argv[1]); raise SystemExit(t.main([]))'
    )
    run = process_sessions.run(
        [sys.executable, '-c', program, str(tree)],
        env={**os.environ, 'NODE_OPTIONS': caller},
        timeout=STOP_LOSS,
        text=True,
    )
    assert run.returncode == typecheck.EVICTED, run.stdout + run.stderr
    assert '0 errors' in run.stdout


def test_basedpyright_still_drops_the_report_after_an_eviction(tmp_path: Path) -> None:
    """While this passes, the layer is needed. When it fails, remove the layer that `kluster.scripts.typecheck` lists."""
    tree = outgrown_tree(tmp_path, stale_ignore=True)

    def run(heap_mb: int) -> subprocess.CompletedProcess[str]:
        options = f'{SMALL_YOUNG_GENERATION} --max-old-space-size={heap_mb}'
        command = [*typecheck.COMMAND, '-p', str(tree)]
        return process_sessions.run(command, env={**os.environ, 'NODE_OPTIONS': options}, timeout=STOP_LOSS, text=True)

    kept = run(typecheck.HEAP_MB).stdout
    assert typecheck.EVICTION not in kept and UNNECESSARY in kept, (
        f"the fixture's ignore is not one basedpyright reports:\n{kept}"
    )
    evicted = run(SMALL_HEAP_MB)
    assert typecheck.EVICTION in evicted.stdout, f'the fixture no longer outgrows {SMALL_HEAP_MB} MiB: resize it'
    assert evicted.returncode in (0, 1), (
        f'the run at {SMALL_HEAP_MB} MiB ended without a verdict (exit {evicted.returncode}): resize the fixture'
    )
    assert UNNECESSARY not in evicted.stdout, (
        'basedpyright reported a stale ignore in a file it parsed again after emptying its type cache: '
        'the defect is fixed, so remove the layer that kluster.scripts.typecheck lists'
    )
