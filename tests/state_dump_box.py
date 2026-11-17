"""The appliance's dump script, run the way the box runs it, over fakes of what it calls.

The script is a file with a shebang and no interpreter behind it that this
repository ships, so the seam a case gets is the one the box has: `PATH`. A
`Box` runs the script as an executable -- its shebang, not `bash <path>` --
with a `PATH` holding one directory of this module's making and nothing
else. In it are a `podman`, an `age` and a `curl` that answer as the case
configured, and, under its own name, every other tool the script starts,
each handing over to the real one. Every one of them records its argv and
what it was wired to before it does anything else, so the record holds
every tool the run started by construction rather than by a list: a tool
the directory does not hold is not found and never starts, and the shell
says so on standard error (`command not found`). What the
recording keeps is the wiring and not only the argv, because it is the
wiring that decides whether the listing reads the archive and where the
archive is written, and the script closes both files as soon as the call
returns.

The spool is a seam like the recipients file and `age`: each box spools
under a directory of its own case's, so what a run leaves there is the case's
to read -- a failed run that kept its plaintext archive is a finding here
rather than a file left behind in the machine's `/var/tmp`. The defaults the
unit relies on are read off the script's text instead
(`test_state_dump._default`).
"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import subprocess as sp
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pinned_tools
import process_sessions
import pytest

from kluster.lib.state_backend import render, settings

SCRIPT = Path(render.__file__).with_name(render.MACHINE) / render.DUMP_SCRIPT

#: The interpreter the script's shebang names, which a run here executes as
#: the box does.
SHEBANG = SCRIPT.read_text().splitlines()[0].removeprefix('#!')

#: What the box's image carries that the script runs and that no fake here
#: stands in for, by the name the script calls it and the binary a run here
#: reaches under that name: the image's set the script's header names, less
#: `bash`, which `SHEBANG` names by path, and `podman`, `age` and `curl`, which
#: are faked. `mise.toml` pins the two whose behavior is the script's own logic
#: -- `gawk`, the box's `awk`, at the appliance's release line, and `jq` -- so a
#: run reaches those and not a host's; the rest a host is trusted to carry, and
#: a host without one fails the run by naming it rather than with the script's
#: own failure.
PINNED_TOOLS = {'awk': 'gawk', 'jq': 'jq'}
HOST_TOOLS = ('cut', 'date', 'mktemp', 'rm', 'sed', 'sha1sum', 'stat')


#: How each executable in the box's directory numbers and records its call
#: before it does anything else.
#:
#: A call claims its number by creating an empty file of that name in the
#: claims directory beside the record (`claims`), and the claim is the one
#: step that is exclusive: under `set -C` bash opens a file that does not
#: exist with `O_EXCL`, so of two calls opening the same name, one creates it
#: and the other's redirection fails. So two calls running at once --
#: recorded tools sit on both sides of the script's pipes -- never share a
#: number: the one whose claim fails goes on to the next. The exclusivity is
#: the shell's, which the box already runs, and not the host's `mkdir`
#: utility, which does not report a lost race on every host (uutils coreutils
#: before 0.11.0 exits 0 when the directory appeared between its check and
#: its create, uutils/coreutils#13970). The record's directory is made only
#: once its number is claimed. The search starts past the claims already
#: there, which are numbered from 1 with no gaps, every number below the
#: first free one being taken.
_RECORD = r"""
claims=$FAKE_RECORD.claims
shopt -s nullglob
taken=("$claims"/*)
shopt -u nullglob
n=${#taken[@]}
while true; do
  n=$((n + 1))
  number=$(printf '%03d' "$n")
  if ( set -C; : > "$claims/$number" ) 2>/dev/null; then break; fi
  if [[ ! -e $claims/$number ]]; then echo "fake $FAKE_TOOL: cannot claim a call under $claims" >&2; exit 98; fi
done
call="$FAKE_RECORD/$number"
mkdir "$call" || { echo "fake $FAKE_TOOL: cannot record a call under $FAKE_RECORD" >&2; exit 98; }
echo "$FAKE_TOOL" > "$call/tool"
printf '%s\0' "$@" > "$call/argv"
readlink "/proc/$$/fd/0" > "$call/stdin_path" || true
readlink "/proc/$$/fd/1" > "$call/stdout_path" || true
"""


def claims(record: Path) -> Path:
    """Where the calls recorded under `record` claim their numbers (`_RECORD`)."""
    return record.with_name(f'{record.name}.claims')


def recorded(path: Path, tool: str, record: Path, body: str) -> None:
    """Write an executable at `path` that records its call under `record` as `tool`'s, then runs `body`.

    The record and `body` run with the host's `PATH`, which the run's own
    leaves out; `box_path` keeps the run's, for a `body` that starts a tool
    as the run would.
    """
    _ = path.write_text(
        f'#!{SHEBANG}\n'
        'set -uo pipefail\n'
        f'FAKE_TOOL={shlex.quote(tool)}\n'
        f'FAKE_RECORD={shlex.quote(str(record))}\n'
        'box_path=$PATH\n'
        f'PATH={shlex.quote(os.environ["PATH"])}\n'
        f'{_RECORD}{body}'
    )
    path.chmod(0o755)


def box_tools(directory: Path, record: Path) -> None:
    """Refuse a host missing what the box's image carries, and put the box's tools in `directory`.

    Each is a stub under the name the script calls it that records its call
    under `record`, then runs the binary a run here reaches -- the pinned one
    for a pinned tool, whichever the host has under that name -- by the path
    it resolves to, so it holds however short the `PATH` it is run under. The
    box's `awk` is `gawk`, and a run here reaches the pinned one under that
    name, whichever `awk` the host has.
    """
    pinned_tools.require(*PINNED_TOOLS.values())
    found = {tool: shutil.which(tool) for tool in HOST_TOOLS}
    missing = [tool for tool, path in found.items() if path is None]
    if not os.access(SHEBANG, os.X_OK):
        missing.insert(0, SHEBANG)
    if missing:
        pytest.fail(
            f'not on this host: {", ".join(missing)}, which the box runs the dump script with, as a run here does'
        )
    reached = {name: pinned_tools.located(binary) for name, binary in PINNED_TOOLS.items()}
    reached |= {tool: path for tool, path in found.items() if path is not None}
    record.mkdir(exist_ok=True)
    claims(record).mkdir(exist_ok=True)
    for name, binary in reached.items():
        recorded(directory / name, name, record, f'PATH=$box_path\nexec {shlex.quote(binary)} "$@"\n')


#: A `pg_restore --list` output, header and all, of a box the backend has
#: opened: its one table, the table's rows, and the key's constraint and
#: index. The header alone is what a box nothing has opened lists.
HEADER = ';\n; Archive created at 2026-09-25 22:32:48 UTC\n;     dbname: pulumi_state\n;\n'
LISTING = HEADER + (
    '217; 1259 16385 TABLE public pulumi_state state_owner\n'
    '3422; 0 16385 TABLE DATA public pulumi_state state_owner\n'
    '3276; 2606 16392 CONSTRAINT public pulumi_state pulumi_state_pkey state_owner\n'
    '3274; 1259 16393 INDEX public pulumi_state_key_prefix_idx state_owner\n'
)

# -- the state table's rows, as `pg_restore --data-only --table=pulumi_state` prints them
#
# The shapes are what the pinned `pulumi` writes into the pinned Postgres
# image, and `test_state_roles.py` holds both parsers to the real thing. Every
# key is `<database>/<path>`; a stack's checkpoint is under `.pulumi/stacks/`.

#: What comes before and after the rows, whatever the rows are.
PREAMBLE = (
    '--\n-- PostgreSQL database dump\n--\n\n'
    "SET statement_timeout = 0;\nSET client_encoding = 'UTF8';\n"
    "SELECT pg_catalog.set_config('search_path', '', false);\n\n"
)
COMPLETE = '--\n-- PostgreSQL database dump complete\n--\n\n'
_COPY = 'COPY public.pulumi_state (key, data, updated_at) FROM stdin;\n'


def key(path: str, database: str = settings.DATABASE) -> str:
    """A state key: the database's name, then the backend's path."""
    return f'{database}/{path}'


def block(*keys: str, head: str = _COPY) -> str:
    """One COPY block: its header, a row per key, and the line that ends it."""
    body = ''.join(f'{name}\t{{"data":"e30="}}\t2026-09-25 22:32:48.846613+00\n' for name in keys)
    return f'{head}{body}\\.\n'


def rows(*keys: str) -> str:
    """The data-only output of an archive whose state table holds these keys."""
    return f'{PREAMBLE}--\n-- Data for Name: pulumi_state; Type: TABLE DATA\n--\n\n{block(*keys)}\n\n{COMPLETE}'


META = key('.pulumi/meta.yaml')
CHECKPOINT = key('.pulumi/stacks/kluster/dns.json')
CHECKPOINT_BAK = f'{CHECKPOINT}.bak'

#: A box nothing has opened: no state table, so no block.
UNOPENED = PREAMBLE + COMPLETE
#: A box the backend has opened -- `pulumi stack ls` is enough -- and nothing
#: has written a stack into: the table, and the backend's meta row alone.
OPENED = rows(META)
#: A box serving one stack: `pulumi stack init` writes its checkpoint and the
#: `.bak` beside it.
SERVING = rows(META, CHECKPOINT_BAK, CHECKPOINT)

ARCHIVE = b'PGDMP-archive-bytes'
CIPHERTEXT = b'age-encrypted bytes'

#: What `b2_authorize_account` v4 answers for the dump key, laid out as the
#: operation's reference page in B2's documentation has it: the key's
#: restriction under `apiInfo.storageApi.allowed`, beside the account's API
#: host, and the token.
ACCOUNT: dict[str, Any] = {
    'accountId': 'account-id',
    'apiInfo': {
        'storageApi': {
            'apiUrl': 'https://api999.backblazeb2.com',
            'allowed': {
                'buckets': [{'id': 'bucket-id', 'name': 'bucket-name'}],
                'capabilities': ['writeFiles'],
                'namePrefix': 'kluster/state/',
            },
        },
    },
    'authorizationToken': 'account-token',
}
UPLOAD_TARGET: dict[str, Any] = {
    'bucketId': 'bucket-id',
    'uploadUrl': 'https://pod-000-1005-03.backblaze.com/b2api/v4/b2_upload_file?cvt=c001_v0001005_t0027&bucket=bucket-id',
    'authorizationToken': 'upload-token',
}

#: The role the dump connects as: the template's superuser, which the
#: container's local socket admits.
_SUPERUSER = re.search(r"\{%- set superuser = '([^']+)' %\}", render.machine_file(render.TEMPLATE))
assert _SUPERUSER is not None, 'the Butane template names no superuser'

#: The environment the unit's `EnvironmentFile=` provides (butane.yaml.j2),
#: with the values the box is rendered with where the template names them.
ENV = {
    'PG_ROLE': _SUPERUSER[1],
    'PG_DATABASE': settings.DATABASE,
    'B2_KEY_ID': 'key-id',
    'B2_KEY': 'key-secret',
    'B2_BUCKET_ID': 'bucket-id',
    'B2_PREFIX': 'kluster/state',
}

#: Each fake: once its call is recorded, answer as the environment says to.
FAKES = {
    'podman': r"""
case " $* " in
  *" pg_dump "*) printf '%s' "$FAKE_ARCHIVE"; exit "$FAKE_PG_STATUS" ;;
  *" pg_restore --data-only "*)
    cat > "$call/stdin"
    printf '%s' "$FAKE_ROWS"
    printf '%s' "$FAKE_COMPLAINT" >&2
    exit "$FAKE_ROWS_STATUS" ;;
  *" pg_restore "*)
    cat > "$call/stdin"
    printf '%s' "$FAKE_LISTING"
    printf '%s' "$FAKE_COMPLAINT" >&2
    exit "$FAKE_LIST_STATUS" ;;
  *) echo "fake podman: unexpected call: $*" >&2; exit 99 ;;
esac
""",
    'age': r"""
cat > "$call/stdin"
printf '%s' "$FAKE_CIPHERTEXT"
exit "$FAKE_AGE_STATUS"
""",
    'curl': r"""
url=${*: -1}
# `-H @-` reads header lines on standard input, as curl does; nothing else
# here reads it, so a call that does not hold it reads none.
previous=
for argument in "$@"; do
  if [[ $previous == -H || $previous == --header ]] && [[ $argument == @- ]]; then
    cat > "$call/stdin"
    break
  fi
  previous=$argument
done
while (( $# )); do
  if [[ $1 == --data-binary && ${2-} == @* ]]; then
    cp "${2#@}" "$call/sent"
    ls "$(dirname "${2#@}")" > "$call/beside"
  fi
  shift
done
if [[ -n $FAKE_CURL_REFUSES && $url == *"$FAKE_CURL_REFUSES"* ]]; then
  printf '%s' '{"code": "bad_auth_token", "message": "Invalid authorization token", "status": 401}'
  exit 22
fi
# The key the box authorizes as is the dump key as the `state-backend` stack
# mints it: confined to its bucket through `bucketIds`, which makes it a
# multi-bucket key in B2's terms (`test_state_backend_stack` holds the
# declaration to that). B2 authorizes such a key on v4 alone; the refusal below
# is the error document B2 answers on v3 (Aetf/kluster-ops#665), numbered for
# the version asked, with curl's `--fail-with-body` exit and the body kept.
if [[ $url =~ /b2api/v([0-9]+)/b2_authorize_account$ ]]; then
  version=${BASH_REMATCH[1]}
  if (( version < 4 )); then
    printf '{"code": "bad_request", "message": "This request is not currently supported on API version number %s", "status": 400}' "$version"
    exit 22
  fi
  if (( version > 4 )); then
    echo "fake curl: no answer for b2_authorize_account v$version: $url" >&2
    exit 99
  fi
  printf '%s' "$FAKE_ACCOUNT"
  exit 0
fi
case $url in
  *b2_authorize_account*) echo "fake curl: b2_authorize_account at no API version: $url" >&2; exit 99 ;;
  *b2_get_upload_url*) printf '%s' "$FAKE_UPLOAD_TARGET" ;;
  *) printf '%s' '{"fileId": "4_z"}' ;;
esac
""",
}


@dataclass(frozen=True)
class Call:
    """One tool the script started: its argv, and what it was wired to."""

    tool: str
    argv: list[str]
    #: What a fake read on standard input, where it read it at all: the
    #: archive `podman` and `age` read, the header lines `curl` read for
    #: `-H @-`. A tool that hands over to the real one keeps nothing here.
    stdin: bytes | None
    #: The file each standard stream was bound to, where it was a file.
    stdin_path: Path | None
    stdout_path: Path | None
    #: `curl` only: the file uploaded with `--data-binary @…`, and the names
    #: in its directory at the moment of the upload.
    sent: bytes | None
    beside: list[str] | None

    def header(self, name: str) -> str | None:
        """A header's value, by case-insensitive name: a `-H` argument's, or a line's that `-H @-` read.

        `curl` only: `-H @-` reads header lines on standard input, which the
        fake keeps as `stdin`.
        """
        lines: list[str] = []
        for flag, value in zip(self.argv, self.argv[1:], strict=False):
            if flag in ('-H', '--header'):
                lines += (self.stdin or b'').decode().splitlines() if value == '@-' else [value]
        for line in lines:
            given, _, value = line.partition(':')
            if given.strip().lower() == name.lower():
                return value.strip()
        return None


def _path(record: Path) -> Path | None:
    target = record.read_text().strip() if record.exists() else ''
    return Path(target) if target.startswith('/') else None


def _read(record: Path) -> bytes | None:
    return record.read_bytes() if record.exists() else None


class Box:
    """One configuration of the fakes, and the runs made under it."""

    def __init__(
        self,
        root: Path,
        *,
        pg: int = 0,
        listing: str = LISTING,
        list_status: int = 0,
        holds: str = SERVING,
        rows_status: int = 0,
        complaint: str = '',
        age: int = 0,
        refuses: str = '',
        recipients: str = 'age1aaa\n\n  age1bbb  \n',
    ) -> None:
        self.bin: Path = root / 'bin'
        self.record: Path = root / 'calls'
        #: Where the script makes its run's directory. Empty between runs: the
        #: script's trap removes what a run put there, on every way out.
        self.spool: Path = root / 'spool'
        self.bin.mkdir()
        box_tools(self.bin, self.record)
        self.spool.mkdir()
        for tool, body in FAKES.items():
            recorded(self.bin / tool, tool, self.record, body)
        recipients_file = root / 'age-recipients.txt'
        _ = recipients_file.write_text(recipients)
        self.env: dict[str, str] = {
            **ENV,
            'PATH': str(self.bin),
            'STATE_DUMP_SPOOL': str(self.spool),
            'STATE_DUMP_RECIPIENTS': str(recipients_file),
            'STATE_DUMP_AGE': str(self.bin / 'age'),
            'FAKE_ARCHIVE': ARCHIVE.decode(),
            'FAKE_PG_STATUS': str(pg),
            'FAKE_LISTING': listing,
            'FAKE_COMPLAINT': complaint,
            'FAKE_LIST_STATUS': str(list_status),
            'FAKE_ROWS': holds,
            'FAKE_ROWS_STATUS': str(rows_status),
            'FAKE_CIPHERTEXT': CIPHERTEXT.decode(),
            'FAKE_AGE_STATUS': str(age),
            'FAKE_CURL_REFUSES': refuses,
            'FAKE_ACCOUNT': json.dumps(ACCOUNT),
            'FAKE_UPLOAD_TARGET': json.dumps(UPLOAD_TARGET),
        }

    def run(self, *args: str, env: dict[str, str] | None = None) -> sp.CompletedProcess[str]:
        """The script, executed as the box executes it.

        A tool the box's directory does not hold fails the case, whatever the
        run's status. Not found, it never starts, so it is never recorded --
        and the record is what says that no tool the run started was handed
        a credential. A call in an argument's substitution (`log "$(stat …)"`)
        is one the run survives, printing bash's `command not found` and
        carrying on. The run's environment sets no locale, so that is the
        message bash prints.
        """
        ran = process_sessions.run([str(SCRIPT), *args], env={**self.env, **(env or {})}, text=True, timeout=30)
        missing = [line for line in ran.stderr.splitlines() if 'command not found' in line]
        if missing:
            pytest.fail(f'the run called a tool the box does not hold: {missing}')
        return ran

    @property
    def calls(self) -> list[Call]:
        found: list[Call] = []
        for call in sorted(self.record.iterdir(), key=lambda path: int(path.name)):
            argv = (call / 'argv').read_bytes().decode().split('\0')[:-1]
            beside = (call / 'beside').read_text().split() if (call / 'beside').exists() else None
            found.append(
                Call(
                    tool=(call / 'tool').read_text().strip(),
                    argv=argv,
                    stdin=_read(call / 'stdin'),
                    stdin_path=_path(call / 'stdin_path'),
                    stdout_path=_path(call / 'stdout_path'),
                    sent=_read(call / 'sent'),
                    beside=beside,
                )
            )
        return found

    def of(self, tool: str) -> list[Call]:
        return [call for call in self.calls if call.tool == tool]

    def spooled(self) -> list[str]:
        """What the runs so far left in the spool, which a finished run leaves empty."""
        return sorted(str(path.relative_to(self.spool)) for path in self.spool.rglob('*'))
