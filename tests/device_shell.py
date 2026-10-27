"""The shell the gateway runs its `#!/bin/sh` scripts under, for the cases that run one.

The gateway's `/bin/sh` is dash, and a host's is whichever `/bin/sh` the host
has, which is not the device's: bash on a workstation, which runs `[[`, arrays
and the other bashisms dash refuses, and a later dash on CI's Ubuntu runners.
So a case that runs a delivered `#!/bin/sh` script does it through `argv`,
which names the dash `mise.toml` pins, never through the host's `/bin/sh`.

A script is the device's dash's by its interpreter: a first line that names
`/bin/sh`, with or without options after it. `argv` passes those options the
way the kernel does, as one argument ahead of the script.

`argv` takes the **program** the script was delivered as -- the name of the
template it was rendered from, less the `.j2`, which is how the gateway
component names a delivered executable after its template -- and refuses a
program whose template is not a `#!/bin/sh` one. `test_device_shell` holds
every `#!/bin/sh` template to a case that runs it this way, and reads that
from the cases' source: the `argv` call is the command of a
`process_sessions.run` or `started` call, written there or unpacked into the
list that is, and its first argument is a name the module imports from
`kluster`, or an attribute of one.
"""

from __future__ import annotations

import re
from pathlib import Path

import pinned_tools
import pytest

ROOT = Path(__file__).parent.parent

#: Where the gateway component's templates live, every executable it
#: delivers to the device among them.
TEMPLATES = ROOT / 'src' / 'kluster' / 'components' / 'gateway' / 'templates'

#: The interpreter a script the device's `/bin/sh` runs names.
INTERPRETER = '/bin/sh'

#: The first line of such a script, without options.
SHEBANG = f'#!{INTERPRETER}'

#: The binary the `http:dash` pin in `mise.toml` installs.
DASH = 'dash'

#: A first line as Linux reads one: the interpreter up to the first blank, and
#: everything after the blanks that follow it, trailing blanks dropped, as one
#: optional argument.
_INTERPRETER_LINE = re.compile(r'#![ \t]*(?P<interpreter>[^ \t]+)(?:[ \t]+(?P<options>.*?))?[ \t]*')


def _options(text: str) -> str | None:
    """The options `text`'s first line hands `/bin/sh`, empty for none; `None` where it names another interpreter."""
    line = _INTERPRETER_LINE.fullmatch(text.split('\n', 1)[0])
    if line is None or line['interpreter'] != INTERPRETER:
        return None
    return line['options'] or ''


def posix_templates() -> list[Path]:
    """Every gateway template whose interpreter is `/bin/sh`: the scripts the device's dash runs."""
    return sorted(path for path in TEMPLATES.iterdir() if path.is_file() and _options(path.read_text()) is not None)


def program(template: Path) -> str:
    """The name the gateway component delivers `template`'s rendering under."""
    return template.name.removesuffix('.j2')


def argv(delivered_as: str, script: Path) -> list[str]:
    """The command that runs `script`, delivered as the program `delivered_as`, the way the device runs it.

    Fails the calling case where `delivered_as` names no `#!/bin/sh`
    template, or where `script` is not a `#!/bin/sh` script, and where the
    pinned dash is not on `PATH`.
    """
    if delivered_as not in {program(template) for template in posix_templates()}:
        pytest.fail(f'{delivered_as} is not rendered from a {SHEBANG} template under {TEMPLATES.relative_to(ROOT)}')
    options = _options(script.read_text())
    if options is None:
        pytest.fail(f'{script} does not start {SHEBANG}, so the device would not run it under its /bin/sh')
    return [pinned_tools.located(DASH), *([options] if options else []), str(script)]
