"""`typecheck`: the gate's type check, basedpyright over the tree under a heap it does not exhaust.

    mise x uv -- uv run typecheck

It runs basedpyright with the project's configuration and `--stats`, under a
Node heap limit of `HEAP_MB`, prints everything basedpyright prints as it
prints it, and exits with basedpyright's own status, but for one case: a run
that logged `EVICTION` and was otherwise clean exits `EVICTED`.

basedpyright empties its type cache when its Node heap nears its limit, and a
file it parses again after that loses one report, and only that one:
`reportUnnecessaryTypeIgnoreComment` (`pyproject.toml`, beside that setting).
A run that empties the cache is complete for type errors and incomplete for
stale ignores, and its exit status does not say which files it skipped.
Node's default heap stops growing with the host's memory at an old space of
4096 MiB, so every runner, CI's included, gets at most a 4288 MB limit, and a
full run of this tree empties the cache several times over under it. Under
`HEAP_MB` a full run empties nothing, with room for the tree to grow. The
limit is a ceiling rather than an allocation: a run's resident memory is set
by the tree. A tree that outgrows it fails here by name, which is why the run
is judged by its log as well as by its status.

`--stats` is what makes basedpyright print the line: it raises the CLI's log
level to the one the line is logged at, as `--verbose` does, without the
hundreds of lines of import resolution `--verbose` adds. What it does add is
a table of timings after the summary.

The heap, the check and the command that carries them are one workaround
layer, for a basedpyright defect whose symptom microsoft/pyright#5202
describes. The layer is this module, `tests/test_typecheck.py`, the
`typecheck` entry in `[project.scripts]` and in AGENTS.md's list of scripts,
the sentences that say why the gate runs it (AGENTS.md's gate bullet, the
Types step of `checks.yml`, the comment beside
`reportUnnecessaryTypeIgnoreComment` in `pyproject.toml`, the exception in
`tests/test_script_imports.py`), and every command line that runs it, which
`git grep -n 'run typecheck'` lists. Outside the repository, anything that
admits a `typecheck` run by free memory is sized for `HEAP_MB`, and goes with
the layer. The layer goes once basedpyright reports an unnecessary ignore in a
file it parsed again, which is when the removal case in
`tests/test_typecheck.py` fails. An emptied cache then costs time and loses
nothing, each of those command lines runs `basedpyright` again, and keeping a
heap for speed is a separate decision.
"""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from collections.abc import Mapping, Sequence

log = logging.getLogger(__name__)

#: The Node heap limit a run gets, in MiB (`--max-old-space-size`).
HEAP_MB = 7168

#: How the line basedpyright logs each time it empties its type cache begins,
#: at the start of the line, before the heap it had used and its limit
#: (`CacheManager.emptyCache`, `packages/pyright-internal/src/analyzer/cacheManager.ts`).
EVICTION = 'Emptying type cache to avoid heap overflow'

#: The exit of a run that emptied its type cache and was otherwise clean: none
#: of basedpyright's `ExitStatus` values (0 to 4, in
#: `packages/pyright-internal/src/pyright.ts`). Node's own exits pass through
#: the launcher too, so the ERROR line, not the number, is what names an
#: eviction.
EVICTED = 5

#: The type check the gate runs: the project's configuration, the whole tree,
#: and the log level that prints `EVICTION`.
COMMAND = (sys.executable, '-m', 'basedpyright', '--stats')


def build_parser() -> argparse.ArgumentParser:
    return argparse.ArgumentParser(
        prog='typecheck',
        description=(
            "Type-check the tree with basedpyright under the project's configuration and a heap the tree does not "
            'exhaust, and fail a run that empties its type cache anyway, which drops the report of '
            'unnecessary `# pyright: ignore` comments.'
        ),
        epilog=f"exit status: basedpyright's own; {EVICTED} when it was 0 and the run emptied its type cache.",
    )


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    _ = build_parser().parse_args(argv)
    log.info('type-checking the tree with basedpyright under a %d MiB heap; a run takes about half a minute', HEAP_MB)
    return check(COMMAND, environment(os.environ))


def environment(caller: Mapping[str, str]) -> dict[str, str]:
    """`caller` with the Node heap limit set to `HEAP_MB`, over any limit `NODE_OPTIONS` already names.

    The caller's other Node options stay. Node takes the last of two limits, so
    the one added here is the one a run gets.
    """
    options = f'{caller.get("NODE_OPTIONS", "")} --max-old-space-size={HEAP_MB}'.strip()
    return {**caller, 'NODE_OPTIONS': options}


def check(command: Sequence[str], env: Mapping[str, str]) -> int:
    """Run `command`, print its output as it comes, and return its status, or `EVICTED` for a clean run that evicted."""
    evictions = 0
    with subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, env=env) as checker:
        for line in checker.stdout or ():
            _ = sys.stdout.write(line)
            _ = sys.stdout.flush()
            if line.startswith(EVICTION):
                evictions += 1
    status = checker.returncode
    if not evictions:
        return status
    log.error(
        'basedpyright emptied its type cache %s, so files it parsed again went unchecked for unnecessary '
        '`# pyright: ignore` comments: the tree needs a larger heap than the one it ran under (NODE_OPTIONS %r), '
        'which is HEAP_MB in kluster.scripts.typecheck',
        'once' if evictions == 1 else f'{evictions} times',
        env.get('NODE_OPTIONS', ''),
    )
    return status or EVICTED
