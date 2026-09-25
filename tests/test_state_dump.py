"""The appliance's nightly dump — the script, the unit that runs it, and what the box can execute.

The script is shell and runs unattended on a box nobody logs into, so the
parts worth pinning are the ones a silent change would break: that the three
B2 calls come in order and carry what B2 validates against, that a failing
step ends the run rather than uploading a truncated object, and that an
archive `pg_restore` cannot read, or one holding no stack checkpoint, never
becomes an object at all. Every case
runs the script as the box runs it (`state_dump_box`): as a file, through its
shebang, with the tools it calls faked on `PATH`.

The same standard reaches the wiring, which is why the later sections read
the Butane template. Where the archive is spooled, where the listing gets its
input, and whether the unit can time out at all are each a choice that fails
silently when it moves: the run still passes, on a box with no watcher, until
the night it does not. And the last section holds the template to what the
box can execute at all, which is the one failure the rest of this file cannot
see -- a script that runs perfectly here under an interpreter the box does
not have.
"""

from __future__ import annotations

import hashlib
import re
import subprocess as sp
from pathlib import Path

import pytest
from state_dump_box import (
    ARCHIVE,
    CHECKPOINT,
    CHECKPOINT_BAK,
    CIPHERTEXT,
    COMPLETE,
    ENV,
    HEADER,
    LISTING,
    META,
    OPENED,
    PREAMBLE,
    SCRIPT,
    SERVING,
    UNOPENED,
    UPLOAD_TARGET,
    Box,
    block,
    key,
    rows,
)

from kluster.scripts.state_backend import config, settings, state

AUTHORIZE_URL = 'https://api.backblazeb2.com/b2api/v3/b2_authorize_account'

TEXT = SCRIPT.read_text()


def _default(variable: str) -> str:
    """The value a `NAME=${SEAM:-default}` line in the script falls back to.

    The seams exist for this suite, so what the box gets is the default; it
    is pinned from the text because a run here cannot reach it.
    """
    found = re.search(rf'^{variable}=\$\{{\w+:-([^}}]+)\}}$', TEXT, re.M)
    assert found is not None, f'{variable} is not a seamed variable of the script'
    return found.group(1)


def _constant(variable: str) -> str:
    """The value a `NAME='literal'` line in the script assigns, read the way `_default` reads a seam."""
    found = re.search(rf"^{variable}='([^']+)'$", TEXT, re.M)
    assert found is not None, f'{variable} is not a constant of the script'
    return found.group(1)


def _left_nothing_behind(box: Box) -> None:
    """The run spooled under the box's own directory, and took everything it put there with it.

    The first half is what keeps the second from passing on a run that never
    reached the spool: the archive `pg_dump` wrote is the proof the directory
    existed. A failed run is the one this matters for -- the archive is the
    whole state in the clear, and a night that left it behind on the box
    leaves the next night's beside it.
    """
    pg = box.of('podman')[0]
    assert pg.stdout_path is not None and pg.stdout_path.parent.parent == box.spool, pg.stdout_path
    assert box.spooled() == []


def test_upload_walks_authorize_then_get_url_then_put(tmp_path: Path) -> None:
    box = Box(tmp_path)

    ran = box.run()

    assert ran.returncode == 0, ran.stderr
    authorize, get_url, put = box.of('curl')
    assert authorize.argv[-1] == AUTHORIZE_URL
    # Basic auth is the key id and secret, not a token.
    assert authorize.argv[authorize.argv.index('-u') + 1] == 'key-id:key-secret'

    # The api url comes from the authorize response, never hardcoded.
    assert get_url.argv[-1] == 'https://api999.backblazeb2.com/b2api/v3/b2_get_upload_url'
    assert get_url.header('Authorization') == 'account-token'
    # Addressed by id: the writeFiles-only key cannot resolve a bucket name.
    assert get_url.argv[get_url.argv.index('--data') + 1] == '{"bucketId":"bucket-id"}'

    assert put.argv[-1] == UPLOAD_TARGET['uploadUrl']
    assert put.header('Authorization') == 'upload-token'
    assert put.sent == CIPHERTEXT
    assert put.header('X-Bz-Content-Sha1') == hashlib.sha1(CIPHERTEXT).hexdigest()
    # The body is the file itself, so curl sets the length B2 validates.
    assert '--data-binary' in put.argv and put.argv[put.argv.index('--data-binary') + 1].startswith('@/')
    # Every call fails rather than hangs, and a large dump outlives the
    # control calls' deadline.
    assert [call.argv[call.argv.index('--max-time') + 1] for call in (authorize, get_url, put)] == ['120', '120', '600']
    # The failure the unit's timeout would otherwise be the only account of.
    assert all('--fail-with-body' in call.argv for call in (authorize, get_url, put))


def test_upload_percent_encodes_the_object_name(tmp_path: Path) -> None:
    box = Box(tmp_path)

    ran = box.run(env={'B2_PREFIX': 'kluster state/2026 08'})

    assert ran.returncode == 0, ran.stderr
    (put,) = box.of('curl')[2:]
    name = put.header('X-Bz-File-Name')
    assert name is not None
    # Slashes stay: they are the object's path separator in B2.
    assert name.startswith('kluster%20state/2026%2008/') and name.endswith('.dump.age')
    assert '%2F' not in name and ' ' not in name


def test_a_refused_b2_call_stops_the_run_with_what_b2_said(tmp_path: Path) -> None:
    """B2's error document is the diagnosis; a status alone is not.

    `bad_auth_token` and `cap_exceeded` are the same curl exit, and the box
    is one nobody logs into to retry the call by hand.
    """
    box = Box(tmp_path, refuses='b2_get_upload_url')

    ran = box.run()

    assert ran.returncode != 0
    assert 'bad_auth_token' in ran.stderr
    assert len(box.of('curl')) == 2, 'nothing is uploaded after a refusal'
    _left_nothing_behind(box)


# -- the stack checkpoints, counted the same way on both sides -----------------

#: Data-only outputs the two copies of the grammar have to count the same way.
#: The grammar is written twice on purpose -- nothing of this repository is
#: installed on the appliance, so the box parses its own archive -- and the
#: hazard of a copy is that it drifts. The rows beside a checkpoint are the
#: ones a loose reading would count: its `.bak`, which `stack rm` leaves
#: behind alone, and the history, backup and lock rows an `up` writes. The
#: `.bak` beside a checkpoint is what makes this a comparison of counts
#: rather than of truthiness: a parser counting it answers 2 there.
STACK_ROWS = [
    ('no state table', UNOPENED, 0),
    ('the meta row alone', OPENED, 0),
    ('a checkpoint and its .bak', rows(META, CHECKPOINT_BAK, CHECKPOINT), 1),
    ('a .bak alone', rows(META, CHECKPOINT_BAK), 0),
    ('a gzip checkpoint', rows(META, key('.pulumi/stacks/kluster/dns.json.gz')), 1),
    (
        'history, backup and lock rows alone',
        rows(
            META,
            key('.pulumi/history/kluster/dns/dns-1.checkpoint.json'),
            key('.pulumi/history/kluster/dns/dns-1.history.json'),
            key('.pulumi/backups/kluster/dns/dns.1.json'),
            key('.pulumi/locks/kluster/dns/0f7e.json'),
        ),
        0,
    ),
    ('two projects', rows(META, CHECKPOINT, key('.pulumi/stacks/physical/physical.json')), 2),
    ("a key under another database's prefix", rows(key('.pulumi/stacks/kluster/dns.json', 'otherdb')), 0),
    (
        'a zstd checkpoint',
        rows(META, key('.pulumi/stacks/kluster/dns.json.zst.bak'), key('.pulumi/stacks/kluster/dns.json.zst')),
        1,
    ),
    ('a zstd .bak alone', rows(META, key('.pulumi/stacks/kluster/dns.json.zst.bak')), 0),
    # The block's boundaries: a checkpoint-shaped key counts only inside the
    # state table's block, never in another table's nor after the `\.` that
    # ends it.
    (
        "another table's block ahead of the state table's",
        PREAMBLE
        + block(key('.pulumi/stacks/kluster/x.json'), head='COPY public.other (key, data) FROM stdin;\n')
        + block(META)
        + COMPLETE,
        0,
    ),
    (
        "a checkpoint-shaped line after the state table's end",
        PREAMBLE + block(META) + f'{key(".pulumi/stacks/kluster/x.json")}\t{{}}\n' + COMPLETE,
        0,
    ),
]


def _count_stacks(data: str) -> int:
    """The box's parser, through the mode the script exposes it under, reading the database the unit names."""
    ran = sp.run(
        [str(SCRIPT), 'count-stacks'],
        input=data,
        env={'PATH': '/usr/bin:/bin', 'PG_DATABASE': ENV['PG_DATABASE']},
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert ran.returncode == 0, ran.stderr
    return int(ran.stdout)


@pytest.mark.parametrize(('what', 'data', 'counted'), STACK_ROWS, ids=[row[0] for row in STACK_ROWS])
def test_the_box_and_the_operator_count_stack_checkpoints_the_same_way(what: str, data: str, counted: int) -> None:
    """The claim that the two dumps are held to one rule is worth only the parity.

    The box counts the keys the operator's side collects, so the two answer
    the same number and not merely the same yes-or-no: an archive either
    side accepted and the other refused would make a nightly object and a
    hand-taken one different artifacts, and a count that drifts is how that
    starts. The box's side is run as the box runs it -- the script's
    `count-stacks` mode is the same `awk` the nightly run reads its own
    archive with -- and both read the database the box is rendered with.
    """
    assert ENV['PG_DATABASE'] == settings.DATABASE
    assert _count_stacks(data) == counted, what
    assert len(state.checkpoints(data, settings.DATABASE)) == counted, what


def test_the_script_and_the_operator_name_the_same_table_and_layout() -> None:
    # Two constants in two languages; a rename on one side would leave the
    # other counting keys in a table that is not there.
    assert _constant('STATE_TABLE') == state.STATE_TABLE
    assert _constant('STACKS') == state.STACKS


# -- the dump ------------------------------------------------------------------


def test_dump_lists_the_archive_and_encrypts_to_every_recipient(tmp_path: Path) -> None:
    box = Box(tmp_path)

    ran = box.run()

    assert ran.returncode == 0, ran.stderr
    pg, listing, _ = box.of('podman')
    assert pg.argv[:2] == ['exec', 'pgstate']
    assert '-Fc' in pg.argv and pg.argv[-2:] == ['operator', 'pulumi_state']
    # The listing runs in the same container, reading the archive on standard
    # input rather than through a mount of the spool directory.
    assert listing.argv == ['exec', '-i', 'pgstate', 'pg_restore', '--list']
    # Blank lines are skipped and surrounding whitespace stripped, or age
    # would be handed a recipient it rejects.
    (encrypt,) = box.of('age')
    assert encrypt.argv == ['--encrypt', '-r', 'age1aaa', '-r', 'age1bbb']
    # And on the box, the age is the one age-install.service pins and the
    # recipients are the file the template writes.
    assert _default('AGE') == '/opt/bin/age'
    assert _default('RECIPIENTS') == '/etc/kluster/age-recipients.txt'
    assert '    - path: /etc/kluster/age-recipients.txt\n' in BUTANE


def test_the_listing_is_fed_the_archive_the_dump_just_wrote(tmp_path: Path) -> None:
    """`-i` in the argv is half of the plumbing; the handle is the other half.

    `pg_restore --list` with nothing on standard input reads an empty stream,
    which it refuses -- so losing the handle turns the check into a step that
    fails every night rather than one that silently passes. What makes it
    worth its own case is that the argv the case above asserts does not
    change when the handle goes.
    """
    box = Box(tmp_path)

    ran = box.run()

    assert ran.returncode == 0, ran.stderr
    pg, listing, read = box.of('podman')
    assert listing.stdin_path == pg.stdout_path
    assert listing.stdin == ARCHIVE
    # And the rows the stack count reads are that archive's too.
    assert read.argv == ['exec', '-i', 'pgstate', 'pg_restore', '--data-only', '--table=pulumi_state', '--file=-']
    assert read.stdin_path == pg.stdout_path
    assert read.stdin == ARCHIVE


def test_the_archive_is_spooled_on_the_disk_rather_than_in_memory(tmp_path: Path) -> None:
    """`/var/tmp` is a statement about this box, not a synonym for `/tmp`.

    The appliance has 1 GB of memory and a 50 GB boot volume with no separate
    `/var`, and a run holds the whole state twice for its duration: the
    archive, and the ciphertext beside it. Spooled to `/tmp` -- a tmpfs sized
    from memory -- a state that outgrows that takes the dump down with an
    ENOSPC on a box that has tens of gigabytes free. Both copies live in a
    directory of the run's own under that spool, and the spool is left as it
    was found.

    The run here spools under the case's own directory, so the box's spool
    is the script's default, read off its text.
    """
    box = Box(tmp_path)

    ran = box.run()

    assert ran.returncode == 0, ran.stderr
    assert _default('SPOOL') == '/var/tmp'
    (pg, _, _), (encrypt,) = box.of('podman'), box.of('age')
    assert pg.stdout_path is not None and encrypt.stdout_path is not None
    assert pg.stdout_path.parent == encrypt.stdout_path.parent
    spool = pg.stdout_path.parent
    assert spool.parent == box.spool and spool.name.startswith('state-dump.')
    _left_nothing_behind(box)


#: What a backend serving no stack dumps to, by whether anything has opened
#: it: `pg_restore --list` and the rows, as the pinned Postgres prints them.
NO_STACK = {
    'nothing has opened it': (HEADER, UNOPENED),
    'the backend has opened it': (LISTING, OPENED),
    'its stacks were removed': (LISTING, rows(META, CHECKPOINT_BAK)),
}


@pytest.mark.parametrize(('listing', 'holds'), list(NO_STACK.values()), ids=list(NO_STACK))
def test_an_archive_holding_no_stack_is_not_uploaded(tmp_path: Path, listing: str, holds: str) -> None:
    """Every object under the dump prefix holds a stack checkpoint (state-backend.md §5).

    A box replaced and not yet restored, or a site before its first `pulumi
    stack init`, dumps to an archive of a plausible size whose restore brings
    nothing back. Uploaded, it would be the newest object -- the one every
    recovery takes -- so the run fails before `age` and before B2, and the
    newest object stays the last dump that held state. The words name the
    rule and both readings, because the box is one nobody logs into to ask.
    """
    box = Box(tmp_path, listing=listing, holds=holds)

    ran = box.run()

    assert ran.returncode != 0
    assert 'holds no stack checkpoint' in ran.stderr
    assert 'nothing was uploaded' in ran.stderr
    assert 'state-backend.md §5' in ran.stderr
    assert 'state-backend restore' in ran.stderr and 'pulumi stack init' in ran.stderr
    assert box.of('age') == [] and box.of('curl') == []
    _left_nothing_behind(box)


#: What a failed rows read printed before it failed: nothing, or -- an archive
#: cut in its last bytes -- the whole block, checkpoint included.
FAILED_READS = {'nothing printed': '', 'every row printed': SERVING}


@pytest.mark.parametrize('holds', list(FAILED_READS.values()), ids=list(FAILED_READS))
def test_rows_that_cannot_be_read_stop_the_run_with_what_pg_restore_said(tmp_path: Path, holds: str) -> None:
    """A failed read is refused whatever it printed, and says why it failed.

    The read is the truncation check for this database: its one data block
    is the state table's, so a cut anywhere past the table of contents fails
    here. A cut in the last bytes prints every row, checkpoint included, and
    then exits 1 -- so the count is positive, and only the read's status,
    carried past the counter by `pipefail`, refuses the archive.
    """
    said = 'pg_restore: error: could not read from input file: end of file'
    box = Box(tmp_path, holds=holds, rows_status=1, complaint=f'{said}\n')

    ran = box.run()

    assert ran.returncode != 0
    assert 'pg_restore --data-only failed' in ran.stderr
    assert said in ran.stderr
    assert box.of('age') == [] and box.of('curl') == []
    _left_nothing_behind(box)


def test_a_listing_that_cannot_be_read_stops_the_run(tmp_path: Path) -> None:
    # `pg_restore` refusing the archive outright means the file is not an
    # archive at all, and must not be read as an unusual but passable answer.
    box = Box(tmp_path, list_status=1, listing='')

    ran = box.run()

    assert ran.returncode != 0
    assert 'pg_restore --list failed' in ran.stderr
    assert box.of('age') == [] and box.of('curl') == []
    _left_nothing_behind(box)


def test_the_refusal_carries_what_pg_restore_said(tmp_path: Path) -> None:
    """A status is not a diagnosis, and this one cannot be reproduced later.

    The listing runs with its output captured, so its stderr is the only
    account of why the archive was refused; the archive itself goes with the
    run's temporary directory, and the box it happened on is one nobody logs
    in to. Dropped here, the reason is gone.
    """
    said = 'pg_restore: error: did not find magic string in file header'
    box = Box(tmp_path, list_status=1, listing='', complaint=f'{said}\n')

    ran = box.run()

    assert ran.returncode != 0
    assert said in ran.stderr
    _left_nothing_behind(box)


def test_the_plaintext_archive_does_not_outlive_the_dump(tmp_path: Path) -> None:
    # It is the whole state in the clear, and the ciphertext beside it is what
    # the upload reads; keeping both to the end of the run buys nothing. What
    # the upload found beside the object it sent is the moment that shows it.
    box = Box(tmp_path)

    ran = box.run()

    assert ran.returncode == 0, ran.stderr
    (put,) = box.of('curl')[2:]
    assert put.beside is not None
    assert [name for name in put.beside if name.endswith(('.dump', '.age'))] == ['state.dump.age']


@pytest.mark.parametrize(('pg', 'age', 'message'), [(1, 0, 'pg_dump failed'), (0, 2, 'age failed')])
def test_dump_fails_when_a_step_fails(tmp_path: Path, pg: int, age: int, message: str) -> None:
    box = Box(tmp_path, pg=pg, age=age)

    ran = box.run()

    assert ran.returncode != 0
    assert message in ran.stderr
    assert box.of('curl') == []
    _left_nothing_behind(box)


def test_the_run_names_the_object_under_the_prefix_and_a_stamp(tmp_path: Path) -> None:
    box = Box(tmp_path)

    ran = box.run()

    assert ran.returncode == 0, ran.stderr
    (put,) = box.of('curl')[2:]
    name = put.header('X-Bz-File-Name')
    assert name is not None
    assert re.fullmatch(r'kluster/state/\d{8}T\d{6}Z\.dump\.age', name), name
    assert f'uploaded {name}' in ran.stdout


def test_the_backend_wait_survives_a_hanging_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Postgres binds 5432 before initdb finishes, so the probe hangs.

    A `TimeoutExpired` escaping the loop ends the wait at exactly the moment
    the appliance is coming up, which is what it did the first time it ran.

    The clock is the test's, advanced only by the wait's own `sleep`, so the
    budget is a number of probes rather than of seconds: a probe that never
    answers runs out of deadline after `600 / 15` of them, and a process
    stalled between computing the deadline and checking it -- swap, a
    contended machine -- moves nothing the wait reads.
    """
    from kluster.scripts.state_backend import provision

    calls: list[int] = []
    clock = [0.0]

    def probe(*_args: object, **_kwargs: object) -> sp.CompletedProcess[str]:
        calls.append(1)
        if len(calls) < 3:
            raise sp.TimeoutExpired(cmd='openssl', timeout=30)
        return sp.CompletedProcess(args=[], returncode=0, stdout='', stderr='')

    def nap(seconds: float) -> None:
        clock[0] += seconds

    monkeypatch.setattr(provision.sp, 'run', probe)
    monkeypatch.setattr(provision.time, 'monotonic', lambda: clock[0])
    monkeypatch.setattr(provision.time, 'sleep', nap)

    assert provision.wait_for_backend('192.0.2.10', timeout=600) is True, f'gave up after {len(calls)} probes'
    assert len(calls) == 3


# -- the unit that runs it ----------------------------------------------------

BUTANE = (config.DEPLOY_DIR / config.TEMPLATE).read_text()

#: The notice a failed run leaves, and a good one removes.
MOTD = '/etc/motd.d/10-state-dump.motd'


def _unit(name: str) -> str:
    """One systemd unit's own lines, out of the Butane template.

    Read as text rather than rendered: rendering wants escrow material and the
    `butane` binary, while every property below is written in the template
    itself. The slice stops at the next entry or at the comment introducing
    it, so one unit's rationale is never read as another unit's contents.

    It refuses to return a slice with no unit in it, because the assertion
    that a unit sets no `PrivateTmp=` is one an empty string also satisfies.
    """
    marker = f'    - name: {name}\n'
    assert marker in BUTANE, f'{name} is not one of the appliance units'
    lines: list[str] = []
    for line in BUTANE.split(marker, 1)[1].splitlines():
        if line.startswith('    - name:') or line.startswith('    #'):
            break
        lines.append(line)
    unit = '\n'.join(lines)
    assert '[Service]' in unit, f'{name} was sliced down to something that is not a unit'
    return unit


def test_the_dump_unit_can_time_out() -> None:
    """A `Type=oneshot` unit has no start timeout unless it asks for one.

    Every step of a dump can hang rather than fail -- a `podman exec` into a
    container that has stopped answering, an upload against a socket nothing
    closes -- and a timer does not start a service whose last run is still
    going. So an untimed hang is not one missed night; it is every night
    after it, with the unit sitting in `activating` and nothing raised.
    """
    unit = _unit('state-dump.service')

    assert 'Type=oneshot' in unit
    assert 'TimeoutStartSec=' in unit


def test_a_failed_dump_reaches_the_next_login() -> None:
    """The box's own account of a failure waits for a human.

    What watches the objects does so from outside, and sees names and
    stamps, never why a run failed (state-backend.md §6). Fedora CoreOS prints `/etc/motd.d/*` at an interactive ssh login — a
    plain `state-backend ssh`; one given a command prints none — and that
    login is how an operator reaches this box, which makes a notice there
    the one channel a failure has. It is worth having only while it means
    "the last run failed", so a successful run takes it down again.
    """
    unit = _unit('state-dump.service')
    assert 'OnFailure=state-dump-failed.service' in unit
    assert 'ExecStartPost=' in unit and MOTD in unit

    notice = _unit('state-dump-failed.service')
    assert MOTD in notice
    # And it says where the rest of the story is.
    assert 'journalctl -u state-dump.service' in notice


def test_the_script_reads_the_environment_the_unit_provides() -> None:
    """The unit's `EnvironmentFile=` and the script's `$NAME`s are one contract, written in two files.

    A variable the template stops writing is an unbound-variable exit on
    the first line that reads it; one the script stops reading is a value
    rendered for nothing. The harness runs the script under the same names,
    so a run here is a run under the box's environment.
    """
    env_file = BUTANE.split('    - path: /etc/kluster/state-dump.env\n', 1)[1].split('\n\n', 1)[0]
    provided = set(re.findall(r'^          (\w+)=', env_file, re.M))
    unit = _unit('state-dump.service')

    assert 'EnvironmentFile=/etc/kluster/state-dump.env' in unit
    assert provided == set(ENV)
    assert {name for name in provided if f'${name}' not in TEXT and f'${{{name}' not in TEXT} == set()


def test_the_dump_unit_spools_to_the_hosts_var_tmp() -> None:
    """`PrivateTmp=` would settle the spool's question from the other file.

    `disconnected` backs the service's `/var/tmp` with a fresh tmpfs, which on
    a 1 GB box is the memory the spool exists to avoid; plain `yes` keeps
    the host's disk behind it but hands the service a private `/var/tmp`, so
    the directory the script named is not the one it writes to. The spool is a
    statement about this box's disk, and neither form of the setting leaves it
    one.
    """
    assert 'PrivateTmp' not in _unit('state-dump.service')
    # Nor does the box move the spool through the script's own seam: the
    # defaults are what it runs on, and every seam is the suite's.
    assert 'STATE_DUMP_' not in BUTANE


# -- what the box can execute at all -------------------------------------------

#: Every binary the appliance template may execute: a `systemd` `Exec*=`
#: head, or the shebang of a file the template writes executable. Fedora
#: CoreOS is immutable and packageless, so the list is what the image ships
#: -- `bash`, `coreutils` (`rm`) and `podman`, each a package in the stable
#: stream's composed set, `manifest-lock.x86_64.json` on the `stable` branch
#: of coreos/fedora-coreos-config -- plus the one path a unit of the same
#: template installs, `/opt/bin/age` (`age-install.service`). No `python3`
#: of any kind is in that lock, which is what this list exists to say
#: (state-backend.md §1).
EXECUTABLES = frozenset({'/usr/bin/bash', '/usr/bin/podman', '/usr/bin/rm', '/opt/bin/age'})

#: The template variables that stand for a file the template writes
#: executable, and the file in `deploy/state-backend/` each is read from
#: (`config.machine`). A variable this table does not name fails the case
#: below, so a new executable file is held to the rule from its first commit.
EXECUTABLE_SOURCES = {'dump_script': config.DUMP_SCRIPT}

EXEC_HEAD = re.compile(r'^\s*Exec\w*=[-+@!:]*(\S+)', re.M)


def _written_files(template: str) -> dict[str, tuple[int, str]]:
    """Each `files:` entry the template writes: its path, mode and first line of contents.

    The template is read as text, so an entry whose contents are a variable
    is resolved through `EXECUTABLE_SOURCES` to the file it is rendered from,
    and only when the entry is executable -- for anything else the first
    line is not read.
    """
    section = template.split('  files:\n', 1)[1].split('\n  links:\n', 1)[0]
    written: dict[str, tuple[int, str]] = {}
    for entry in re.split(r'^    - path: ', section, flags=re.M)[1:]:
        path = entry.split('\n', 1)[0].strip()
        found = re.search(r'^      mode: (0[0-7]+)$', entry, re.M)
        mode = int(found.group(1), 8) if found is not None else 0o644
        first = entry.split('inline: |\n', 1)[1].split('\n', 1)[0].strip()
        variable = re.fullmatch(r'\{\{\s*(\w+)\s*(\|.*)?\}\}', first)
        if variable is not None and mode & 0o111:
            assert variable.group(1) in EXECUTABLE_SOURCES, (
                f'{path} is rendered from {first}, which names no known file'
            )
            first = (config.DEPLOY_DIR / EXECUTABLE_SOURCES[variable.group(1)]).read_text().split('\n', 1)[0]
        written[path] = (mode, first)
    return written


def executed(template: str) -> dict[str, str]:
    """What the template executes, by where it says so.

    An `Exec*=` head that names a file the template itself writes is followed
    to that file's shebang, since that is what systemd's exec reaches; every
    other head, and every shebang of a file written executable, stands as it
    is.
    """
    written = _written_files(template)
    targets: dict[str, str] = {}
    for head in EXEC_HEAD.findall(template):
        if head in written:
            mode, _ = written[head]
            assert mode & 0o111, f'{head} is executed but not written executable'
            continue
        targets[f'Exec line {head}'] = head
    for path, (mode, first) in written.items():
        if mode & 0o111:
            assert first.startswith('#!'), f'{path} is executable and has no shebang'
            targets[f'shebang of {path}'] = first[2:].split()[0]
    return targets


def test_the_appliance_executes_only_what_the_image_ships() -> None:
    """The rule of state-backend.md §1: nothing the box runs names a binary it does not have.

    The suite runs the dump script on a workstation, where any interpreter
    is present; the box has none, and a shebang naming one fails at exec with
    `203/EXEC` and "No such file or directory" against a file that exists.
    The allowlist is the only place in the gate that knows what the image
    ships, so it is the only place that can see the failure before the first
    nightly does.
    """
    targets = executed(BUTANE)

    assert targets, 'the template executes nothing, which is not this template'
    assert {where: head for where, head in targets.items() if head not in EXECUTABLES} == {}
    # The rule reaches the dump script through its shebang, not by accident
    # of the template text: the variable it is rendered from is resolved.
    assert targets['shebang of /usr/local/bin/state-dump'] == '/usr/bin/bash'
