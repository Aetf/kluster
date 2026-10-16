"""The prose checker's server needs the stack `mise.toml` gives it, and this says when it stops needing it.

ltex-ls-plus parses a markdown block's inline math with a `java.util.regex`
lazy loop that runs from a lone `$` to the end of the block, and Java's
regex engine recurses once per character of it. Past a few hundred words
after the `$`, the server's worker overflows the default thread stack; the
server swallows the `StackOverflowError`, publishes no diagnostics, and
`ltex-cli-plus` waits for them for good (kluster-ops#465). `mise.toml`'s
`[env]` gives every JVM `mise x` starts a stack that clears it.

That line is a workaround, and the two cases here are its edges. The first
runs the pinned checker on the reproducer with the default stack and holds
that it still overflows: when a release fixes the parser, it fails, and
both the line and this file go. The second holds that the stack the line
gives is one the reproducer is checked under.

The reproducer is `data/ltex-lone-dollar.txt`, a draft of a bullet of
`docs/declarative/physical.md` that hung the checker every run. It is kept as
`.txt` so the prose step, which checks every markdown file, never reads it;
a case copies it to a `.md` to check it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tomllib
from pathlib import Path

import pinned_tools
import process_sessions

ROOT = Path(__file__).parent.parent
REPRODUCER = Path(__file__).parent / 'data' / 'ltex-lone-dollar.txt'

#: How long either case lets the checker run: a stop-loss well inside the
#: 60 s case bound. On a workstation the overflow is thrown about 4 s in,
#: and the check under the larger stack finishes in about 7 s.
STOP_LOSS = 40

#: How often the first case looks at the server's exception log, as the
#: bound of each wait on the checker: the overflow has no other outward sign.
LOOK = 0.5

#: `ltex-cli-plus`'s exits: no finding, and findings. Either is a verdict.
VERDICTS = (0, 3)


def _check(tmp_path: Path) -> list[str]:
    """The checker's argv over a copy of the reproducer, under a configuration of nothing."""
    document = tmp_path / 'reproducer.md'
    _ = shutil.copyfile(REPRODUCER, document)
    configuration = tmp_path / 'ltex.json'
    _ = configuration.write_text('{}')
    return [pinned_tools.located('ltex-cli-plus'), f'--client-configuration={configuration}', str(document)]


def _environment(java_opts: str) -> dict[str, str]:
    # The archive's launchers run `$JAVA_HOME/bin/java` when it is set; empty,
    # they run the JRE the archive bundles, which is the pinned one.
    return {**os.environ, 'JAVA_HOME': '', 'JAVA_OPTS': java_opts}


def _overflowed(logs: Path) -> bool:
    return any('java/lang/StackOverflowError' in log.read_text() for log in logs.glob('exceptions-*.log'))


def test_the_pinned_checker_still_overflows_on_a_lone_dollar_with_the_default_stack(tmp_path: Path) -> None:
    """While this passes, `mise.toml`'s `JAVA_OPTS` is needed. When it fails, remove that line and this file."""
    logs = tmp_path / 'logs'
    logs.mkdir()
    argv = _check(tmp_path)
    # Every exception the JVMs throw is logged, one file per JVM: the
    # overflow is the server's, and nothing the client prints shows it.
    environment = _environment(f'-Xlog:exceptions=info:file={logs}/exceptions-%p.log')

    exited: int | None = None
    with process_sessions.started(
        argv, env=environment, stdout=process_sessions.DEVNULL, stderr=process_sessions.DEVNULL
    ) as command:
        for _ in range(int(STOP_LOSS / LOOK)):
            if _overflowed(logs):
                break
            try:
                exited = command.wait(timeout=LOOK)
                break
            except subprocess.TimeoutExpired:
                continue

    assert exited is None, (
        f'the pinned ltex-cli-plus checked {REPRODUCER.name} on the default stack and exited {exited}: '
        'the overflow is fixed upstream, so remove JAVA_OPTS from mise.toml and this file'
    )
    assert _overflowed(logs), f'neither an overflow nor an exit within {STOP_LOSS}s'


def test_the_stack_mise_gives_the_checker_clears_the_overflow(tmp_path: Path) -> None:
    java_opts = tomllib.loads((ROOT / 'mise.toml').read_text())['env']['JAVA_OPTS']
    argv = _check(tmp_path)

    checked = process_sessions.run(argv, env=_environment(java_opts), timeout=STOP_LOSS)

    assert checked.returncode in VERDICTS, checked.stdout + checked.stderr
