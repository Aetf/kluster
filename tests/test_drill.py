"""`drill rebuild`: operations.md §3.5's steps as the script runs them, against stand-ins for every tool.

Every command a step starts goes to `Estate`, which answers as the estate
would -- `pulumi`, `kubectl`, `kubeseal`, `talosctl`, `doggo`, `gh`,
`credentials` -- and records what was started, in which mode and under which
environment. The operator is `Operator`, answering each question by what it
asks. What is under test is the script's own: which commands run in which
order, what is refused before anything runs, what a reading is judged, and
what reaches the run's directory.

The seam between §3.5 and the script is held here too: the runbook's step
headings and `steps.STEPS` are one list, each heading naming its own
`drill rebuild <n>`. And the scope's patterns are held to the URNs the
`physical` program registers, the side of that seam the drill does not write.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import tomllib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

import process_sessions
import pytest
import pytest_asyncio
from engine_urns import engine_urns
from mock_monitor import declaring
from physical_installation import install

from kluster import conventions
from kluster.conventions import schedule, sealed
from kluster.scripts import manual_run
from kluster.scripts.credentials import devices, oci_recovery
from kluster.scripts.drill import capacity, chain, cli, readings, record, scope, steps, tools
from kluster.scripts.drill.readings import NoReading
from kluster.scripts.drill.record import Verdict
from kluster.scripts.drill.tools import Completed, Environments, Mode
from kluster.scripts.perf import cluster
from kluster.stacks import physical

ROOT = Path(__file__).parent.parent
RUNBOOK = ROOT / 'docs' / 'operations.md'

#: A fixed moment every case's clock reads.
NOW = dt.datetime(2026, 11, 21, 8, 0, tzinfo=dt.UTC)

#: What the stand-in prints under `--show-secrets` and nowhere else.
SECRET = 'a-value-only-show-secrets-prints'

OLD_CERTIFICATE = '-----BEGIN CERTIFICATE-----\nold\n-----END CERTIFICATE-----\n'
NEW_CERTIFICATE = '-----BEGIN CERTIFICATE-----\nnew\n-----END CERTIFICATE-----\n'

NODES = {'cp1': '10.20.0.11', 'cp2': '10.20.0.12', 'cp3': '10.20.0.13'}
VOLUME = next(iter(conventions.NODE_VOLUMES))


# --------------------------------------------------------------------------
# The stand-ins.
# --------------------------------------------------------------------------

type Reply = Completed | Callable[[tuple[str, ...]], Completed]


@dataclass(frozen=True)
class Call:
    argv: tuple[str, ...]
    mode: Mode
    env: dict[str, str]


def _holds(argv: Sequence[str], needle: Sequence[str]) -> bool:
    """Whether every word of `needle` is in `argv`, in order."""
    words = iter(argv)
    return all(any(word == wanted for word in words) for wanted in needle)


def done(out: str = '', *, exit: int = 0, err: str = '') -> Completed:  # noqa: A002 -- named as `Completed` names it
    return Completed((), exit, out, err)


#: A finished Job's conditions as Kubernetes 1.36's controller leaves them: two
#: of them true.
COMPLETE = [('SuccessCriteriaMet', 'True'), ('Complete', 'True')]
FAILED = [('FailureTarget', 'True'), ('Failed', 'True')]


@dataclass
class Jobs:
    """The monitoring namespace's Jobs, by name, each with its conditions; one not named here has completed."""

    conditions: dict[str, list[tuple[str, str]]] = field(default_factory=dict[str, list[tuple[str, str]]])
    active: list[str] = field(default_factory=list[str])

    def held(self, name: str) -> list[tuple[str, str]]:
        return self.conditions.get(name, COMPLETE)

    def wait(self, argv: tuple[str, ...]) -> Completed:
        """`kubectl wait` on a Job, as the pinned release answers it."""
        (target,) = [word.removeprefix('job/') for word in argv if word.startswith('job/')]
        for word in argv:
            if word.startswith('--for=condition='):
                kind = word.removeprefix('--for=condition=')
                return done() if (kind, 'True') in self.held(target) else done(exit=1, err='timed out waiting')
            matched = sum(1 for _, status in self.held(target) if status == 'True')
            if word.startswith('--for=jsonpath=') and '@.status=="True"' in word and matched > 1:
                return done(exit=1, err='given jsonpath expression matches more than one value')
        return done(exit=1, err='no condition this stand-in reads')

    def get(self, argv: tuple[str, ...]) -> Completed:
        name = argv[argv.index('job') + 1]
        conditions = [{'type': kind, 'status': status} for kind, status in self.held(name)]
        return done(json.dumps({'metadata': {'name': name}, 'status': {'conditions': conditions}}))

    def listed(self, argv: tuple[str, ...]) -> Completed:
        del argv
        items = [{'metadata': {'name': name}, 'status': {'active': 1}} for name in self.active]
        return done(json.dumps({'items': items}))


@dataclass
class Estate:
    """Every tool a step starts, answering by the words of its command line; the last rule given wins."""

    rules: list[tuple[tuple[str, ...], Reply]] = field(default_factory=list[tuple[tuple[str, ...], Reply]])
    calls: list[Call] = field(default_factory=list[Call])
    jobs: Jobs = field(default_factory=Jobs)

    def answer(self, *needle: str, reply: Reply) -> None:
        self.rules.insert(0, (needle, reply))

    def __call__(self, argv: Sequence[str], *, env: dict[str, str] | Any, cwd: Path, mode: Mode) -> Completed:
        del cwd
        self.calls.append(Call(tuple(argv), mode, dict(env)))
        for needle, reply in self.rules:
            if _holds(argv, needle):
                got = reply(tuple(argv)) if callable(reply) else reply
                return Completed(tuple(argv), got.exit, got.out, got.err)
        return Completed(tuple(argv), 127, '', f'no stand-in answers {" ".join(argv)}')

    def ran(self, *needle: str) -> list[Call]:
        return [call for call in self.calls if _holds(call.argv, needle)]


@dataclass
class Operator:
    """The person at the terminal: each question answered by the first key it contains, in turn."""

    answers: dict[str, list[str]] = field(default_factory=dict[str, list[str]])
    asked: list[str] = field(default_factory=list[str])

    def __call__(self, question: str) -> str | None:
        self.asked.append(question)
        for key, queue in self.answers.items():
            if key in question:
                return queue.pop(0) if queue else None
        return None


def _resource(urn: str, *, protect: bool = False, secret: str = 'ciphertext') -> dict[str, Any]:
    return {'urn': urn, 'type': urn.split('::')[2].rsplit('$', 1)[-1], 'protect': protect, 'outputs': {'value': secret}}


#: `physical`'s state as the stand-in exports it: the node volume, outside
#: the scope and protected; the three instances, each in an availability
#: domain of its own; and the dedicated VIP's reserved address.
PROTECTED_URN = 'urn:pulumi:physical::kluster::oci:Core/volume:Volume::kluster-hath-cache'
IN_SCOPE_URN = f'urn:pulumi:physical::kluster::{scope.CLOUD_NODES}$oci:Core/instance:Instance::kluster-cp1'
INSTANCES = [f'kluster-{node}' for node in conventions.CLOUD_NODES]
DOMAINS = [f'ZRbp:PHX-AD-{index}' for index in range(1, len(INSTANCES) + 1)]
VIP_OCID = 'ocid1.publicip.vip1'
VOLUME_OCID = 'ocid1.volume.hath-cache'


def exported(*, private_ip_id: str = '') -> Callable[[tuple[str, ...]], Completed]:
    """The export of that state, its reserved address on `private_ip_id`; every value secret under `--show-secrets`."""

    def export(argv: tuple[str, ...]) -> Completed:
        secret = SECRET if '--show-secrets' in argv else '[secret]'
        volume = _resource(PROTECTED_URN, protect=True, secret=secret)
        volume |= {'id': VOLUME_OCID, 'outputs': {'displayName': f'kluster-{VOLUME}', 'value': secret}}
        instances = [
            {
                'urn': f'urn:pulumi:physical::kluster::{scope.CLOUD_NODES}$oci:Core/instance:Instance::{name}',
                'type': steps.INSTANCE,
                'id': f'ocid1.instance.{name}',
                'outputs': {'displayName': name, 'availabilityDomain': domain, 'value': secret},
            }
            for name, domain in zip(INSTANCES, DOMAINS, strict=True)
        ]
        vip = {
            'urn': f'urn:pulumi:physical::kluster::{scope.CLOUD_NODES}$oci:Core/publicIp:PublicIp::{steps.VIP}',
            'type': steps.PUBLIC_IP,
            'id': VIP_OCID,
            'outputs': {'displayName': steps.VIP, 'privateIpId': private_ip_id},
        }
        return done(json.dumps({'deployment': {'resources': [volume, *instances, vip]}}))

    return export


def _left_read(argv: tuple[str, ...]) -> Completed:
    """`credentials derived oci-physical read` over the reserved address and the volume, as its own lines print them."""
    del argv
    lines = [
        oci_recovery.Reading(
            'public-ip', VIP_OCID, steps.VIP, 'AVAILABLE', ('address=203.0.113.20', 'lifetime=RESERVED', 'assigned=-')
        ),
        oci_recovery.Reading('volume', VOLUME_OCID, f'kluster-{VOLUME}', 'AVAILABLE', ('attached-to=-',)),
    ]
    return done('\n'.join(line.line() for line in lines) + '\n')


def _discovered(uuid: str | None, partition_uuid: str | None, *, label: str = f'u-{VOLUME}') -> str:
    spec = {'partition_label': label, 'name': 'xfs', 'uuid': uuid, 'partition_uuid': partition_uuid}
    other = {'partition_label': 'EPHEMERAL', 'name': 'xfs', 'uuid': 'e', 'partition_uuid': 'e'}
    return '\n'.join(json.dumps({'node': '10.20.0.11', 'spec': each}, indent=2) for each in (other, spec))


def estate() -> Estate:
    """The estate as a rebuild meets it: every read answers whole, every write succeeds."""
    stand = Estate()
    outputs = dict(zip(readings.OUTPUTS, ('203.0.113.10', '2001:db8::10', '203.0.113.20', '2001:db8::a'), strict=True))
    stand.answer('gh', reply=done())
    stand.answer('pulumi', reply=done())
    stand.answer('pulumi', 'stack', 'export', reply=exported())
    for name, value in outputs.items():
        stand.answer('pulumi', 'stack', 'output', name, reply=done(value + '\n'))
    stand.answer('pulumi', 'stack', 'output', 'node_private_ips', '--json', reply=done(json.dumps(NODES)))
    anchors = {
        readings.ANCHORS[0]: '203.0.113.10\n',
        readings.ANCHORS[1]: '2001:db8::10\n',
        readings.ANCHORS[2]: '203.0.113.20\n',
        readings.ANCHORS[3]: '',
    }
    for (name, kind), printed in anchors.items():
        stand.answer('doggo', '--short', name, kind, reply=done(printed))
    stand.answer('talosctl', 'get', 'discoveredvolumes', reply=done(_discovered('fs-uuid', 'gpt-guid')))
    stand.answer('kubeseal', reply=done(OLD_CERTIFICATE))
    stand.answer('kubectl', reply=done())
    stand.answer(
        'kubectl',
        'get',
        '--raw',
        cluster.history('/metrics'),
        reply=done('vm_rows{type="storage/big"} 100\nvm_rows{type="storage/small"} 20\n'),
    )
    stand.answer(
        'kubectl',
        'get',
        '--raw',
        cluster.history(readings.EARLIEST_QUERY),
        reply=done('{"data":{"result":[{"value":[0,"1700000000"]}]}}'),
    )
    stand.answer('kubectl', 'get', 'jobs', reply=stand.jobs.listed)
    stand.answer('kubectl', 'get', 'job', '-o', 'json', reply=stand.jobs.get)
    stand.answer('kubectl', 'get', 'job', steps.DRILL_BACKUP_JOB, '-o', 'jsonpath={.status.succeeded}', reply=done('1'))
    stand.answer(
        'kubectl',
        'wait',
        reply=lambda argv: stand.jobs.wait(argv) if any(word.startswith('job/') for word in argv) else done(),
    )
    stand.answer('ssh', reply=done())
    stand.answer('credentials', reply=done())
    stand.answer('credentials', 'derived', 'oci-physical', 'read', reply=_left_read)
    stand.answer('perf', reply=done())
    return stand


def operator(**extra: list[str]) -> Operator:
    """The operator of a deliberate run: a capacity reading in which every AD passes, and the go."""
    return Operator(
        {
            'paste its lines': [f'{DOMAINS[0]} AVAILABLE null', *(f'{each} AVAILABLE 3' for each in DOMAINS[1:]), ''],
            'Type `go`': ['go'],
            **extra,
        }
    )


@dataclass
class Drill:
    """One checkout's rebuild, run against the stand-ins."""

    checkout: Path
    stand: Estate
    person: Operator
    said: list[str] = field(default_factory=list[str])

    @property
    def directory(self) -> Path:
        return self.checkout / record.DIRECTORY

    def record(self) -> record.Record:
        return record.Record.open(self.directory)

    def configure(self, context: steps.Context) -> None:
        context.clock = lambda: NOW
        context.kubeconfig = self.checkout / 'home' / 'kubeconfig'
        context.talosconfig = self.checkout / 'home' / 'talosconfig'

    def run(self, step: str | None = None, *, require_main: Callable[[Path], None] = lambda _: None) -> int:
        return cli.rebuild(
            self.checkout,
            step,
            base={'PATH': os.environ.get('PATH', ''), 'PULUMI_CONFIG_PASSPHRASE': 'the-stack-passphrase'},
            helm=self.checkout / 'helm',
            ask=self.person,
            say=self.said.append,
            exec=self.stand,
            physical=lambda: 'physicals-own-passphrase',
            require_main=require_main,
            configure=self.configure,
        )

    def passed(self, *keys: str, parts: dict[str, Sequence[str]] | None = None) -> None:
        """Record `keys` as passed, and `parts` of a step as passed within it."""
        kept = self.record()
        for key in keys:
            kept.started(key, NOW)
            kept.ended(key, Verdict.PASS, NOW)
        for key, names in (parts or {}).items():
            for name in names:
                kept.part_passed(key, name)

    def kept(self, kind: readings.Kind[Any], reading: object) -> None:
        self.record().keep_baseline(kind.name, kind.keep(reading))

    def transcripts(self) -> str:
        return '\n'.join(path.read_text() for path in sorted(self.directory.iterdir()))


@pytest.fixture
def drill(tmp_path: Path) -> Drill:
    checkout = tmp_path / 'kluster'
    checkout.mkdir()
    _ = (checkout / 'Pulumi.yaml').write_text((ROOT / 'Pulumi.yaml').read_text())
    return Drill(checkout=checkout, stand=estate(), person=operator())


def _whole_baselines(drill: Drill) -> None:
    drill.kept(readings.PROTECTED, frozenset({PROTECTED_URN}))
    drill.kept(readings.OUTSIDE, 'digest')
    drill.kept(readings.SEALING, OLD_CERTIFICATE)
    drill.kept(readings.METRICS, readings.Metrics(rows=120, earliest='1700000000'))


# --------------------------------------------------------------------------
# The seam with operations.md §3.5.
# --------------------------------------------------------------------------


def _rebuild_section() -> str:
    text = RUNBOOK.read_text()
    return text[text.index('**§3.5 ') : text.index('\n## 4. ')]


def test_the_runbooks_steps_are_the_scripts_steps_in_order() -> None:
    """§3.5's `**Step N: …**` headings are `steps.STEPS`, in order: a step added to either side alone fails here."""
    headings = re.findall(r'\*\*Step (\d+):', _rebuild_section())

    assert headings == [step.key for step in steps.STEPS]


def test_each_step_of_the_runbook_opens_with_the_command_that_runs_it() -> None:
    """Each step's paragraph names `drill rebuild <n>` with its own number, before the next step begins."""
    section = _rebuild_section()
    starts = [match.start() for match in re.finditer(r'\*\*Step \d+:', section)]
    paragraphs = [section[start:end] for start, end in zip(starts, [*starts[1:], len(section)], strict=True)]

    named = [re.findall(r'`drill rebuild (\w+)`', ' '.join(paragraph.split()))[:1] for paragraph in paragraphs]

    assert named == [[step.key] for step in steps.STEPS]


def test_the_runbooks_rebuild_carries_no_shell_block() -> None:
    """The commands are the script's: §3.5 keeps no indented block of them."""
    # A block inside the section's list item is indented eight spaces; a
    # nested list item is too, behind its marker.
    blocks = re.findall(r'\n\n( {8}(?![-*] |\d+\. )\S[^\n]*)', _rebuild_section())

    assert blocks == []


# --------------------------------------------------------------------------
# Every step starts from the forge's main.
# --------------------------------------------------------------------------

GIT_ENV = {
    'GIT_CONFIG_GLOBAL': os.devnull,
    'GIT_CONFIG_NOSYSTEM': '1',
    'GIT_AUTHOR_NAME': 'probe',
    'GIT_AUTHOR_EMAIL': 'probe@example.invalid',
    'GIT_COMMITTER_NAME': 'probe',
    'GIT_COMMITTER_EMAIL': 'probe@example.invalid',
}


def _git(*args: str, cwd: Path) -> None:
    _ = process_sessions.run(['git', *args], cwd=cwd, env=os.environ | GIT_ENV, text=True, check=True, timeout=60)


@pytest.fixture
def behind(drill: Drill, tmp_path: Path) -> Drill:
    """The drill's checkout, a clone whose forge has moved on since, and has been fetched."""
    forge, other = tmp_path / 'forge.git', tmp_path / 'other'
    _git('init', '-q', '--bare', '-b', 'main', str(forge), cwd=tmp_path)
    _git('init', '-q', '-b', 'main', cwd=drill.checkout)
    _git('remote', 'add', 'origin', str(forge), cwd=drill.checkout)
    _git('add', 'Pulumi.yaml', cwd=drill.checkout)
    _git('commit', '-q', '-m', 'first', cwd=drill.checkout)
    _git('push', '-q', 'origin', 'main', cwd=drill.checkout)
    _git('clone', '-q', str(forge), str(other), cwd=tmp_path)
    _ = (other / 'landed.txt').write_text('a change that landed\n')
    _git('add', 'landed.txt', cwd=other)
    _git('commit', '-q', '-m', 'landed', cwd=other)
    _git('push', '-q', 'origin', 'main', cwd=other)
    _git('fetch', '-q', 'origin', cwd=drill.checkout)
    drill.passed('0')
    return drill


@pytest.mark.parametrize('step', [step.key for step in steps.SEQUENCE])
def test_a_step_from_a_working_copy_behind_main_is_refused_before_any_command_runs(behind: Drill, step: str) -> None:
    exit_status = behind.run(step, require_main=manual_run.require_main)

    assert exit_status == cli.REFUSED
    assert behind.stand.calls == []
    assert behind.person.asked == []
    assert behind.record().verdict(step) is (Verdict.PASS if step == '0' else None)
    assert any('refused before any command ran' in line for line in behind.said)


@pytest.mark.parametrize('step', [step.key for step in steps.SEQUENCE[1:]])
def test_a_named_step_is_refused_until_step_0_has_passed(drill: Drill, step: str) -> None:
    assert drill.run(step) == cli.REFUSED

    assert drill.stand.calls == []
    assert any(steps.LOSS_MODE in line for line in drill.said)


def test_a_run_under_a_claude_directory_is_refused(drill: Drill, tmp_path: Path) -> None:
    drill.checkout = tmp_path / '.claude' / 'workspaces' / 'probe'
    drill.checkout.mkdir(parents=True)

    assert drill.run('0') == cli.REFUSED
    assert drill.stand.calls == []


# --------------------------------------------------------------------------
# Step 0: the baselines, and the go.
# --------------------------------------------------------------------------


def test_a_whole_estate_takes_every_baseline_and_goes(drill: Drill) -> None:
    assert drill.run('0') == cli.DONE

    assert drill.record().verdict('0') is Verdict.PASS
    assert (
        steps.unwhole_baselines(steps.Context(drill.checkout, drill.record(), cast('Any', None), drill.person, print))
        == []
    )
    assert drill.stand.ran('gh', 'workflow', 'disable', chain.WORKFLOW)
    assert drill.stand.ran('kubectl', 'create', 'job', steps.DRILL_BACKUP_JOB)


def _no_answer(argv: tuple[str, ...]) -> Completed:
    del argv
    return done(exit=1, err='connection timed out')


#: Each way a baseline read comes back not whole, as the stand-in's answer
#: that makes it so.
NOT_WHOLE: dict[str, tuple[tuple[str, ...], Reply]] = {
    'protected: an export with nothing protected': (
        ('pulumi', 'stack', 'export'),
        done(json.dumps({'deployment': {'resources': [_resource(IN_SCOPE_URN)]}})),
    ),
    'outside: an export that failed': (('pulumi', 'stack', 'export', '--show-secrets'), _no_answer),
    'anchors: a query with no answer': (('doggo', '--short', readings.ANCHORS[0][0], 'A'), _no_answer),
    'anchors: a name with no address': (('doggo', '--short', readings.ANCHORS[1][0], 'AAAA'), done('')),
    'ids: no partition found': (('talosctl', 'get', 'discoveredvolumes'), done(_discovered('u', 'p', label='other'))),
    'ids: a partition with no GUID': (('talosctl', 'get', 'discoveredvolumes'), done(_discovered('u', None))),
    'ids: a read that failed': (('talosctl', 'get', 'discoveredvolumes'), _no_answer),
    'sealing: two certificates': (('kubeseal',), done(OLD_CERTIFICATE + NEW_CERTIFICATE)),
    'sealing: a fetch that failed': (('kubeseal',), _no_answer),
    'metrics: a read that failed, so no rows': (('kubectl', 'get', '--raw', cluster.history('/metrics')), _no_answer),
    'metrics: no earliest sample': (
        ('kubectl', 'get', '--raw', cluster.history(readings.EARLIEST_QUERY)),
        done('{"data":{"result":[]}}'),
    ),
    'outputs: one that holds nothing': (('pulumi', 'stack', 'output', readings.OUTPUTS[3]), done('\n')),
}


@pytest.mark.parametrize('case', list(NOT_WHOLE))
def test_the_go_is_refused_over_each_kind_of_reading_that_is_not_whole(drill: Drill, case: str) -> None:
    needle, reply = NOT_WHOLE[case]
    drill.stand.answer(*needle, reply=reply)

    assert drill.run('0') == cli.STOPPED

    assert drill.record().verdict('0') is Verdict.NO_READING
    assert not any('Type `go`' in question for question in drill.person.asked)
    assert not drill.stand.ran('kubectl', 'create', 'job')


def test_a_baseline_not_whole_refuses_the_go_however_the_step_resumes(drill: Drill) -> None:
    """The go reads the kept baselines itself, so a resume that skips the readings meets the same refusal."""
    drill.passed(parts={'0': ['freeze', 'converged', 'baselines', 'backup', 'gateway-health', 'capacity']})
    _whole_baselines(drill)
    drill.kept(readings.OUTSIDE, NoReading('the export of physical with its secrets failed'))

    assert drill.run() == cli.STOPPED

    assert drill.record().verdict('0') is Verdict.NO_READING
    assert not any('Type `go`' in question for question in drill.person.asked)


def test_a_capacity_reading_that_does_not_pass_stands_the_run_down(drill: Drill) -> None:
    drill.person.answers['paste its lines'] = [
        f'{DOMAINS[0]} OUT_OF_HOST_CAPACITY 0',
        *(f'{each} AVAILABLE 3' for each in DOMAINS[1:]),
        '',
    ]

    assert drill.run('0') == cli.STOPPED

    assert drill.stand.ran('gh', 'workflow', 'enable', chain.WORKFLOW)
    suspended = [call.argv[-1] for call in drill.stand.ran('kubectl', 'patch', 'cronjob')]
    assert suspended.count('{"spec":{"suspend":false}}') == len(schedule.METRICS_BACKUPS)
    assert drill.record().passed_parts('0') == (), 'a run that stood down resumes at the freeze'


# --------------------------------------------------------------------------
# What reaches the run's directory.
# --------------------------------------------------------------------------


def test_a_value_printed_under_show_secrets_never_reaches_the_run_directory(drill: Drill) -> None:
    drill.stand.answer(
        'pulumi', 'stack', 'output', '--show-secrets', reply=done(f'kubeconfig: {SECRET}\n', err=f'warning {SECRET}')
    )
    assert drill.run('0') == cli.DONE
    drill.passed('1', '2', '3', '4', '5', '6')
    _ = drill.run('7')

    written = drill.transcripts()
    assert written, 'the run wrote nothing to look through'
    assert SECRET not in written
    assert record.WITHHELD in written
    assert SECRET in (drill.checkout / 'home' / 'kubeconfig').read_text(), 'the client configuration was not written'


def test_every_command_runs_under_cis_helm_environment_and_its_stacks_passphrase(drill: Drill, tmp_path: Path) -> None:
    helm = tmp_path / 'helm'
    environments = Environments(
        base={
            'PULUMI_CONFIG_PASSPHRASE': 'the-stack-passphrase',
            'PULUMI_CONFIG_PASSPHRASE_FILE': '/elsewhere/passphrase',
            'HELM_REPOSITORY_CONFIG': '/caller/repositories.yaml',
            'PULUMI_K8S_HELM_REPOSITORY_CACHE': '/caller/cache',
        },
        helm=helm,
        physical=lambda: 'physicals-own-passphrase',
    )

    handed = {stack: environments.pulumi(stack) for stack in (steps.PHYSICAL, steps.DNS, steps.APPS)}

    assert {stack: env.get('PULUMI_CONFIG_PASSPHRASE') for stack, env in handed.items()} == {
        steps.PHYSICAL: 'physicals-own-passphrase',
        steps.DNS: 'the-stack-passphrase',
        steps.APPS: 'the-stack-passphrase',
    }
    assert ['PULUMI_CONFIG_PASSPHRASE_FILE' in env for env in handed.values()] == [False] * len(handed)
    for env in [*handed.values(), environments.tool()]:
        assert {name for name in env if name.startswith(manual_run.HELM_STEERING)} == set(manual_run.HELM_HOMES)
        assert all(Path(env[name]).is_relative_to(helm) for name in manual_run.HELM_HOMES)


# --------------------------------------------------------------------------
# Steps 1 to 5.
# --------------------------------------------------------------------------


def _reading(at: dt.datetime, *, failing: str | None = None, leaving_out: str | None = None) -> dict[str, object]:
    """A capacity reading taken `at`, one line per AD of the instances in state but `leaving_out`."""
    return capacity.Capacity(
        tuple(
            capacity.Domain(each, 'OUT_OF_HOST_CAPACITY' if each == failing else capacity.AVAILABLE, None)
            for each in DOMAINS
            if each != leaving_out
        ),
        at,
    ).dump()


def _instances(drill: Drill) -> None:
    """Steps 0 to 2 passed, and the capacity reading of the hour."""
    drill.passed('0', '1', '2')
    drill.record().keep_fact('capacity', _reading(NOW))


def test_step_3_is_refused_over_a_capacity_reading_in_which_an_ad_fails(drill: Drill) -> None:
    _instances(drill)
    drill.record().keep_fact('capacity', _reading(NOW, failing=DOMAINS[1]))
    drill.person.answers['paste its lines'] = [f'{DOMAINS[1]} OUT_OF_HOST_CAPACITY 0', '']

    assert drill.run('3') == cli.STOPPED

    assert drill.stand.ran('credentials') == []


def test_a_capacity_reading_that_leaves_out_an_instances_domain_is_refused(drill: Drill) -> None:
    _instances(drill)
    drill.record().keep_fact('capacity', _reading(NOW, leaving_out=DOMAINS[2]))
    drill.person.answers['paste its lines'] = [f'{each} AVAILABLE null' for each in DOMAINS[:2]] + ['']
    drill.person.answers['Type the names'] = [' '.join(INSTANCES)]

    assert drill.run('3') == cli.STOPPED

    assert drill.stand.ran('credentials') == []
    assert any(DOMAINS[2] in line for line in drill.said)


def test_a_capacity_paste_naming_a_domain_twice_is_not_read() -> None:
    with pytest.raises(capacity.Unreadable):
        _ = capacity.parse([f'{DOMAINS[0]} AVAILABLE null', f'{DOMAINS[0]} AVAILABLE 3'], NOW)


def test_step_3_is_refused_over_a_capacity_reading_that_is_stale(drill: Drill) -> None:
    _instances(drill)
    drill.record().keep_fact('capacity', _reading(NOW - dt.timedelta(hours=2)))
    drill.person.answers['paste its lines'] = ['']
    drill.person.answers['Type the names'] = [' '.join(INSTANCES)]

    assert drill.run('3') == cli.STOPPED

    assert drill.stand.ran('credentials') == []
    assert drill.record().verdict('3') is Verdict.FAIL


def test_a_resume_hours_after_the_capacity_part_passed_terminates_nothing(drill: Drill) -> None:
    """The termination is licensed as it starts, not by the part that passed when the reading was fresh."""
    _instances(drill)
    drill.record().part_passed('3', 'capacity')
    drill.record().keep_fact('capacity', _reading(NOW - dt.timedelta(hours=5)))
    drill.person.answers['Type the names'] = [' '.join(INSTANCES)]

    assert drill.run() == cli.STOPPED

    assert any('Type the names' in question for question in drill.person.asked)
    assert drill.stand.ran('credentials', 'derived', 'oci-physical', 'terminate') == []


def test_the_termination_is_refused_from_a_working_copy_that_left_main_during_the_step(drill: Drill) -> None:
    _instances(drill)
    drill.person.answers['Type the names'] = [' '.join(INSTANCES)]
    checks: list[Path] = []

    def moved(checkout: Path) -> None:
        checks.append(checkout)
        if len(checks) > 1:
            raise manual_run.CheckRefused("the forge's main moved", why=manual_run.Unready.BEHIND_MAIN)

    assert drill.run('3', require_main=moved) == cli.STOPPED

    assert len(checks) == 2
    assert drill.stand.ran('credentials', 'derived', 'oci-physical', 'terminate') == []


def test_a_whole_step_3_terminates_and_reads_the_loss(drill: Drill) -> None:
    _instances(drill)
    drill.person.answers['Type the names'] = [' '.join(INSTANCES)]

    assert drill.run('3') == cli.DONE

    assert drill.stand.ran('credentials', 'derived', 'oci-physical', 'read', VIP_OCID, VOLUME_OCID)


def test_step_3_terminates_only_the_instances_confirmed_by_name(drill: Drill) -> None:
    _instances(drill)
    drill.person.answers['Type the names'] = [' '.join(INSTANCES[:2]), ' '.join(INSTANCES)]

    assert drill.run('3') == cli.STOPPED
    assert drill.stand.ran('credentials', 'terminate') == []
    _ = drill.run()

    (terminated,) = drill.stand.ran('credentials', 'derived', 'oci-physical', 'terminate')
    assert terminated.argv[-3:] == tuple(f'ocid1.instance.{name}' for name in INSTANCES)
    assert terminated.mode is Mode.ATTACHED


def test_a_target_list_off_its_count_runs_nothing(drill: Drill) -> None:
    drill.passed('0')
    listed = '\n'.join(
        f'    URN: urn:pulumi:physical::kluster::{scope.TALOS_DAY1}$talos:x:Y::n{index}' for index in range(5)
    )
    drill.stand.answer('pulumi', 'stack', '--show-urns', reply=done(listed))

    assert drill.run('1') == cli.STOPPED

    assert drill.stand.ran('pulumi', 'destroy') == []


def test_a_plan_stepping_outside_the_scope_is_not_applied(drill: Drill) -> None:
    drill.passed('0', '1', '2', '3', '4')
    listed = '\n'.join(
        f'    URN: urn:pulumi:physical::kluster::libvirt:index/volume:Volume::{name}'
        for name in (scope.WORKER_IMAGE, scope.WORKER_DISK)
    )
    drill.stand.answer('pulumi', 'stack', '--show-urns', reply=done(listed))
    beyond = 'urn:pulumi:physical::kluster::oci:Core/volume:Volume::kluster-hath-cache'
    drill.stand.answer(
        'pulumi', 'preview', '--json', reply=done(json.dumps({'steps': [{'op': 'replace', 'urn': beyond}]}))
    )

    assert drill.run('5') == cli.STOPPED

    assert drill.stand.ran('pulumi', 'up') == []
    assert drill.record().fact('scope checks') == [[beyond]]


# --------------------------------------------------------------------------
# Step 7's comparison of the node volume's partition.
# --------------------------------------------------------------------------


def _ids(uuid: str | None, partition_uuid: str | None, *, found: int = 1) -> readings.Ids:
    return readings.Ids(tuple(readings.Partition('xfs', uuid, partition_uuid) for _ in range(found)))


IDS_CASES: dict[str, tuple[readings.Reading[readings.Ids], readings.Reading[readings.Ids], bool]] = {
    'matching UUIDs': (_ids('fs', 'gpt'), _ids('fs', 'gpt'), True),
    'a baseline read on the boot that provisioned the volume': (_ids(None, 'gpt'), _ids('fs', 'gpt'), True),
    'a rebuilt partition with a fresh GUID': (_ids('fs', 'gpt'), _ids('fs', 'another-gpt'), False),
    'a rebuilt filesystem with a fresh UUID': (_ids('fs', 'gpt'), _ids('another-fs', 'gpt'), False),
    'a rebuilt partition discovery found empty': (_ids(None, 'gpt'), _ids(None, 'gpt'), False),
    '0 found on the rebuilt node': (_ids('fs', 'gpt'), _ids('fs', 'gpt', found=0), False),
    'a read of the rebuilt node that failed': (_ids('fs', 'gpt'), NoReading('failed'), False),
    'a baseline with no GUID': (_ids('fs', None), _ids('fs', None), False),
}


@pytest.mark.parametrize('case', list(IDS_CASES))
def test_the_node_volumes_partition_is_judged_by_step_7s_rule(case: str) -> None:
    baseline, rebuilt, passes = IDS_CASES[case]

    assert readings.ids_pass(baseline, rebuilt) is passes


def test_a_null_uuid_is_read_from_talos_as_none() -> None:
    read = readings.ids(VOLUME, Completed((), 0, _discovered('null', 'gpt')))

    assert read == _ids(None, 'gpt')


def test_an_empty_outside_never_compares_equal_to_an_empty_baseline() -> None:
    empty = Completed((), 0, json.dumps({'deployment': {'resources': []}}))
    failed = Completed((), 1, '', 'the backend did not answer')

    readings_taken = [readings.outside(each, scope.SCOPE) for each in (empty, failed)]

    assert all(isinstance(each, NoReading) for each in readings_taken)
    assert not any(readings.same(baseline, now) for baseline in readings_taken for now in readings_taken)


# --------------------------------------------------------------------------
# Steps 8 and 9: the dispatched chain, the controller, the sitting.
# --------------------------------------------------------------------------


def _run_row(
    run_id: int, *, head: str = 'h', conclusion: str = 'success', event: str = 'workflow_dispatch'
) -> dict[str, Any]:
    return {
        'databaseId': run_id,
        'url': f'https://forge.invalid/runs/{run_id}',
        'headSha': head,
        'status': 'completed',
        'conclusion': conclusion,
        'event': event,
    }


MERGE = 'merge-commit-sha'


def _step_9_estate(drill: Drill) -> None:
    """Steps 0 to 8 passed, and the estate as step 9 meets it."""
    drill.passed('0', '1', '2', '3', '4', '5', '6', '7', '8')
    _whole_baselines(drill)
    drill.record().keep_fact('dispatched before', [1])
    stand = drill.stand
    stand.answer(
        'gh', 'run', 'list', '--event', 'workflow_dispatch', reply=done(json.dumps([_run_row(1), _run_row(2)]))
    )
    stand.answer(
        'gh', 'run', 'list', '--event', 'push', reply=done(json.dumps([_run_row(5, head=MERGE, event='push')]))
    )
    stand.answer(
        'gh',
        'run',
        'view',
        reply=done(
            json.dumps(
                {
                    'attempt': 1,
                    'conclusion': 'success',
                    'jobs': [{'name': job, 'conclusion': 'success'} for job in chain.APPLYING_JOBS],
                }
            )
        ),
    )
    stand.answer('gh', 'pr', 'view', reply=done(json.dumps({'state': 'MERGED', 'mergeCommit': {'oid': MERGE}})))
    stand.answer('kubeseal', reply=done(NEW_CERTIFICATE))
    nodes = [
        {'metadata': {'name': name}, 'status': {'conditions': [{'type': 'Ready', 'status': 'True'}]}}
        for name in conventions.ALL_NODES
    ]
    stand.answer('kubectl', 'get', 'nodes', '-o', 'json', reply=done(json.dumps({'items': nodes})))
    stand.answer('kubectl', 'get', 'daemonset', 'kube-proxy', reply=done(exit=1, err='Error from server (NotFound)'))
    key = drill.checkout / 'home' / 'piped.pem'
    key.parent.mkdir(parents=True, exist_ok=True)
    _ = key.write_text('a piped value\n')
    piped = sum(len(steps.piped(command)) for command in steps.sitting())
    drill.person.answers.update(
        {
            'piped in': [str(key)] * piped,
            'carry the legacy': [steps.DECOMMISSIONED],
            'type its number': ['42'],
            'available memory': ['9 GiB'],
            'peak recorder': [''],
        }
    )


def test_a_dispatched_run_is_the_one_listed_after_the_dispatch_and_not_before() -> None:
    before = [1, 2]
    after = [chain.Run(number, f'u{number}', 'h', 'queued', '', 'workflow_dispatch') for number in (1, 2, 7)]

    assert chain.created(before, after) == after[2]
    assert chain.created(before, after[:2]) is None
    with pytest.raises(chain.Ambiguous):
        _ = chain.created([1], after)


APPLIED_CASES: dict[str, tuple[list[dict[str, Any]], int | None]] = {
    "the merge's own run": ([_run_row(5, head=MERGE, event='push'), _run_row(6, event='push')], 5),
    "the run that replaced the merge's pending one": (
        [
            _run_row(4, event='push'),
            _run_row(5, head=MERGE, conclusion='cancelled', event='push'),
            _run_row(6, head='renovate', event='push'),
        ],
        6,
    ),
    "no run of the merge's yet": ([_run_row(4, event='push')], None),
}


@pytest.mark.parametrize('case', list(APPLIED_CASES))
def test_pass_condition_3_reads_the_run_that_applied_the_merge(case: str) -> None:
    listed, applied = APPLIED_CASES[case]

    found = chain.applied(MERGE, chain.runs(listed))

    assert (found.id if found is not None else None) == applied


def test_a_whole_step_9_seals_after_the_controller_answers_and_reads_both_chains(drill: Drill) -> None:
    _step_9_estate(drill)

    assert drill.run('9') == cli.DONE

    commands = [call.argv for call in drill.stand.calls]
    first_seal = next(index for index, argv in enumerate(commands) if argv[:2] == ('credentials', 'derived'))
    available = next(index for index, argv in enumerate(commands) if '--for=condition=Available' in argv)
    assert available < first_seal
    watched = [call.argv[3] for call in drill.stand.ran('gh', 'run', 'watch')]
    assert watched == ['2', '5']


def test_the_sitting_does_not_start_while_the_controller_is_not_available(drill: Drill) -> None:
    _step_9_estate(drill)
    drill.stand.answer('kubectl', 'wait', '--for=condition=Available', reply=done(exit=1, err='timed out'))

    assert drill.run('9') == cli.STOPPED

    assert drill.stand.ran('credentials') == []


def test_the_sitting_does_not_start_while_the_certificate_is_the_baselines(drill: Drill) -> None:
    _step_9_estate(drill)
    drill.stand.answer('kubeseal', reply=done(OLD_CERTIFICATE))

    assert drill.run('9') == cli.STOPPED

    assert drill.stand.ran('credentials') == []


def test_a_carry_that_changes_the_certificate_served_stops_the_step_before_anything_is_sealed(drill: Drill) -> None:
    _step_9_estate(drill)
    served = iter([NEW_CERTIFICATE, OLD_CERTIFICATE.replace('old', 'legacy')])
    drill.stand.answer('kubeseal', reply=lambda _: done(next(served)))
    drill.person.answers['carry the legacy'] = [steps.CARRIED]

    assert drill.run('9') == cli.STOPPED

    assert drill.stand.ran('kubectl', 'rollout', 'status', f'deployment/{conventions.SEALING_CONTROLLER}')
    assert drill.stand.ran('credentials') == []
    assert any('bring-up.md §3' in line for line in drill.said)


def test_the_sitting_is_every_command_that_seals_a_census_row_each_once_in_census_order() -> None:
    sitting = steps.sitting()
    order = [value.sealed_by for value in sealed.VALUES.values()]

    assert set(sitting) == set(order)
    assert len(sitting) == len(set(sitting))
    assert [order.index(command) for command in sitting] == sorted(order.index(command) for command in sitting)


def test_a_census_row_added_joins_the_next_sitting(drill: Drill, monkeypatch: pytest.MonkeyPatch) -> None:
    added = sealed.SealedValue(
        name='a-new-row',
        namespace='probe',
        keys=('k',),
        stack='apps',
        sealed_by=sealed.SealingCommand('a-new-row', 'seal'),
    )
    monkeypatch.setattr(sealed, 'VALUES', {**sealed.VALUES, added.name: added})
    _step_9_estate(drill)

    assert drill.run('9') == cli.DONE

    sealed_with = [call.argv[2:] for call in drill.stand.ran('credentials', 'derived') if call.argv[2] != 'sync']
    assert sealed_with[-1] == ('a-new-row', 'seal')
    assert len(sealed_with) == len({value.sealed_by for value in sealed.VALUES.values()})
    assert all(call.mode is Mode.ATTACHED for call in drill.stand.ran('credentials'))


def test_a_sitting_that_stops_resumes_at_the_command_it_stopped_at(drill: Drill) -> None:
    _step_9_estate(drill)
    stopping = steps.sitting()[2]
    drill.stand.answer('credentials', 'derived', stopping.row, stopping.verb, reply=done(exit=1))

    assert drill.run('9') == cli.STOPPED
    drill.stand.answer('credentials', 'derived', stopping.row, stopping.verb, reply=done())
    drill.stand.calls.clear()
    _ = drill.run()

    resumed = [call.argv[2:4] for call in drill.stand.ran('credentials', 'derived') if call.argv[2] != 'sync']
    assert resumed[0] == (stopping.row, stopping.verb)


# --------------------------------------------------------------------------
# Resuming.
# --------------------------------------------------------------------------


def test_a_run_resumes_at_the_first_part_not_recorded_as_a_pass(drill: Drill) -> None:
    drill.passed('0', '1', '2', '3', parts={'4': ['refresh']})

    _ = drill.run()

    assert '-- refresh' not in drill.said
    assert drill.said.index('-- recorded') == drill.said.index('step 4: the state records the loss') + 1


def test_naming_a_step_runs_it_from_its_first_part(drill: Drill) -> None:
    drill.passed('0', '1', '2', '3', parts={'4': ['refresh']})

    _ = drill.run('4')

    assert drill.said.index('-- refresh') == drill.said.index('step 4: the state records the loss') + 1


# --------------------------------------------------------------------------
# The scope, against the URNs the `physical` program registers.
# --------------------------------------------------------------------------


@pytest_asyncio.fixture(scope='module', loop_scope='module')
async def registered(tmp_path_factory: pytest.TempPathFactory) -> list[str]:
    """Every URN of the `physical` program, at the engine's form: each type qualified by its whole chain of parents."""
    with pytest.MonkeyPatch.context() as patch:
        recorded = await install(patch, tmp_path_factory.mktemp('checkout'))
        async with declaring():
            await physical.main()
    return list(engine_urns(recorded.registrations).values())


TARGETS = {'day-1 chain': scope.DAY1, "worker's disk": scope.DISK, "worker's image and disk": scope.WORKER}


@pytest.mark.parametrize('name', list(TARGETS))
def test_each_steps_targets_find_their_count_in_the_program(registered: list[str], name: str) -> None:
    targets = TARGETS[name]

    assert len(targets.of(registered)) == targets.count


def test_the_losss_targets_find_their_count_in_the_program(registered: list[str]) -> None:
    sets = sum(1 for urn in registered if scope.BACKEND_SETS.search(urn))
    targets = scope.loss(sets)

    assert sets > 0
    assert len(targets.of(registered)) == targets.count


def test_the_scope_holds_every_target_and_leaves_out_what_the_rebuild_keeps(registered: list[str]) -> None:
    targeted = {urn for targets in (*TARGETS.values(), scope.loss(1)) for urn in targets.of(registered)}
    kept = [
        urn
        for urn in registered
        if re.search(
            r'oci:Core/volume:Volume::|backendSet:BackendSet::|networkLoadBalancer:NetworkLoadBalancer::|Core/vcn:Vcn::',
            urn,
        )
    ]

    assert {urn for urn in targeted if not scope.SCOPE.search(urn)} == set()
    assert kept, 'the program registers none of what the rebuild keeps'
    assert [urn for urn in kept if scope.SCOPE.search(urn)] == []


def test_step_9_reads_the_run_that_replaced_the_merges_pending_one(drill: Drill) -> None:
    _step_9_estate(drill)
    pushed = [_run_row(5, head=MERGE, conclusion='cancelled', event='push'), _run_row(6, head='renovate', event='push')]
    drill.stand.answer('gh', 'run', 'list', '--event', 'push', reply=done(json.dumps(pushed)))
    drill.stand.answer(
        'gh', 'run', 'view', '5', reply=done(json.dumps({'attempt': 1, 'conclusion': 'cancelled', 'jobs': []}))
    )

    assert drill.run('9') == cli.DONE

    assert [call.argv[3] for call in drill.stand.ran('gh', 'run', 'watch')] == ['2', '6']
    assert record.as_mapping(drill.record().fact('applied'))['id'] == 6


def test_a_dispatch_starts_step_9_over(drill: Drill) -> None:
    drill.passed('0', '1', '2', '3', '4', '5', '6', '7', '9', parts={'9': ['dispatched']})
    drill.record().keep_progress(steps.STEP_9, [str(steps.sitting()[0])])
    drill.stand.answer('gh', 'run', 'list', '--event', 'workflow_dispatch', reply=done(json.dumps([_run_row(1)])))

    assert drill.run('8') == cli.DONE

    assert drill.record().verdict(steps.STEP_9) is None
    assert drill.record().progress(steps.STEP_9) == ()


# --------------------------------------------------------------------------
# The review round: piped commands, refusals that keep secrets, Jobs, the freeze.
# --------------------------------------------------------------------------


def _piped_by_devices() -> dict[str, list[devices.Field]]:
    """Every `credentials derived <row> record` whose record, read off `devices` itself, takes a multiline field."""
    # `credentials`' dispatcher takes `DEVICES` first, so it wins a shared name.
    records: dict[str, devices.Device | devices.SealedRecord] = {**devices.SEALED_RECORDS, **devices.DEVICES}
    return {
        name: fields
        for name, record_of in records.items()
        if (fields := [each for each in record_of.fields if each.multiline])
    }


def test_each_command_of_the_sitting_whose_record_is_piped_is_given_a_source(drill: Drill) -> None:
    piped = {
        command: _piped_by_devices()[command.row]
        for command in steps.sitting()
        if command.verb == 'record' and command.row in _piped_by_devices()
    }
    assert piped, 'the sitting holds no piped command to hold the drill to'
    _step_9_estate(drill)

    assert drill.run('9') == cli.DONE

    for command, fields in piped.items():
        (ran,) = drill.stand.ran('credentials', 'derived', command.row, command.verb)
        assert all(each.flag in ran.argv for each in fields), (str(command), ran.argv)


def test_a_piped_command_run_in_the_operators_own_terminal_is_recorded_once_they_say_it_exited_0(drill: Drill) -> None:
    piped = [command for command in steps.sitting() if steps.piped(command)]
    _step_9_estate(drill)
    drill.person.answers['piped in'] = [steps.EXITED_0] * len(piped)

    assert drill.run('9') == cli.DONE

    for command in piped:
        assert drill.stand.ran('credentials', 'derived', command.row, command.verb) == []
        assert str(command) in drill.record().progress(steps.STEP_9)


def test_a_piped_command_given_no_file_stops_the_sitting_there(drill: Drill) -> None:
    (first, *_) = [command for command in steps.sitting() if steps.piped(command)]
    _step_9_estate(drill)
    drill.person.answers['piped in'] = [str(drill.checkout / 'no-such-file')]

    assert drill.run('9') == cli.STOPPED

    assert drill.stand.ran('credentials', 'derived', first.row, first.verb) == []
    assert str(first) not in drill.record().progress(steps.STEP_9)


def test_the_sitting_is_refused_from_a_working_copy_that_left_main_during_the_wait(drill: Drill) -> None:
    _step_9_estate(drill)
    checks: list[Path] = []

    def moved(checkout: Path) -> None:
        checks.append(checkout)
        if len(checks) > 1:
            raise manual_run.CheckRefused("the forge's main moved", why=manual_run.Unready.BEHIND_MAIN)

    assert drill.run('9', require_main=moved) == cli.STOPPED

    assert drill.stand.ran('credentials') == []


def test_a_failed_show_secrets_command_names_its_exit_alone(drill: Drill) -> None:
    drill.stand.answer(
        'pulumi', 'stack', 'export', '--show-secrets', reply=done(exit=1, err=f'error: decrypting {SECRET} failed')
    )

    assert drill.run('0') == cli.STOPPED

    assert SECRET not in drill.transcripts()
    assert not any(SECRET in line for line in drill.said)


def test_a_backup_job_holding_two_true_conditions_is_read_as_ended(drill: Drill) -> None:
    """A job that failed while the drill waited ends the wait as one that completed does; the drill's own must complete."""
    drill.stand.jobs.active = ['vmbackup-recent-1']
    drill.stand.jobs.conditions['vmbackup-recent-1'] = FAILED

    assert drill.run('0') == cli.DONE
    assert drill.stand.ran('kubectl', 'create', 'job', steps.DRILL_BACKUP_JOB)

    drill.stand.jobs.conditions[steps.DRILL_BACKUP_JOB] = FAILED
    assert drill.run('0') == cli.STOPPED


def test_a_step_0_that_stops_after_the_freeze_says_so_and_stands_down_on_the_word(drill: Drill) -> None:
    drill.stand.answer('pulumi', 'preview', reply=done(exit=1, err='1 change planned'))
    drill.person.answers[steps.STAND_DOWN] = [steps.STAND_DOWN]

    assert drill.run('0') == cli.STOPPED

    (question,) = [each for each in drill.person.asked if steps.STAND_DOWN in each]
    assert 'deploy.yml is disabled' in question
    assert drill.stand.ran('gh', 'workflow', 'enable', chain.WORKFLOW)
    assert drill.record().passed_parts('0') == ()


def test_a_step_0_left_frozen_says_what_is_still_frozen(drill: Drill) -> None:
    drill.stand.answer('ssh', reply=done('gw: BGP session down\n', exit=1))

    assert drill.run('0') == cli.STOPPED

    (question,) = [each for each in drill.person.asked if steps.STAND_DOWN in each]
    assert 'both backup schedules are suspended' in question
    assert drill.stand.ran('gh', 'workflow', 'enable') == []
    assert any('left frozen' in line for line in drill.said)


def test_a_stand_down_that_did_not_take_says_what_is_still_frozen(drill: Drill) -> None:
    drill.stand.answer('gh', 'workflow', 'enable', reply=done(exit=1, err='HTTP 502'))
    drill.person.answers['paste its lines'] = [f'{DOMAINS[0]} OUT_OF_HOST_CAPACITY 0', '']

    assert drill.run('0') == cli.STOPPED

    assert any('still frozen' in line and 'gh workflow enable' in line for line in drill.said)


def _loss_state(drill: Drill, *, private_ip_id: str) -> None:
    drill.passed('0', '1', '2', '3', parts={'4': ['refresh']})
    urns = [
        'urn:pulumi:physical::kluster::kluster:cloud:Balancer$oci:NetworkLoadBalancer/backendSet:BackendSet::set',
        f'urn:pulumi:physical::kluster::{scope.CLOUD_NODES}$oci:Core/publicIp:PublicIp::{steps.VIP}',
        *(
            f'urn:pulumi:physical::kluster::{scope.CLOUD_NODES}$oci:NetworkLoadBalancer/backend:Backend::b-{node}'
            for node in conventions.CLOUD_NODES
        ),
    ]
    drill.stand.answer('pulumi', 'stack', '--show-urns', reply=done('\n'.join(f'    URN: {urn}' for urn in urns)))
    drill.stand.answer('pulumi', 'stack', 'export', reply=exported(private_ip_id=private_ip_id))


@pytest.mark.parametrize(('private_ip_id', 'verdict'), [('', Verdict.PASS), ('ocid1.privateip.old', Verdict.FAIL)])
def test_step_4_reads_the_reserved_ip_with_no_private_ip(drill: Drill, private_ip_id: str, verdict: Verdict) -> None:
    _loss_state(drill, private_ip_id=private_ip_id)

    _ = drill.run()

    assert drill.record().verdict('4') is verdict


def test_a_node_reporting_disk_pressure_fails_the_gate(drill: Drill) -> None:
    _step_9_estate(drill)
    nodes = [
        {
            'metadata': {'name': name},
            'status': {'conditions': [{'type': 'Ready', 'status': 'True'}, {'type': 'DiskPressure', 'status': 'True'}]},
        }
        for name in conventions.ALL_NODES
    ]
    drill.stand.answer('kubectl', 'get', 'nodes', '-o', 'json', reply=done(json.dumps({'items': nodes})))

    assert drill.run('9') == cli.STOPPED

    assert drill.record().passed_parts(steps.STEP_9)[-1] == 'chains'


# --------------------------------------------------------------------------
# The second review round.
# --------------------------------------------------------------------------


@pytest.mark.parametrize('later', ['1', '3', steps.STEP_9])
def test_step_0_is_not_run_again_once_a_later_step_has_started(drill: Drill, later: str) -> None:
    drill.passed('0')
    drill.record().started(later, NOW)

    assert drill.run('0') == cli.REFUSED

    assert drill.stand.calls == []
    assert any(f'step {later} has started' in line for line in drill.said)
    assert drill.record().verdict('0') is Verdict.PASS
    assert drill.record().times(later)[0] is not None


def _interrupted(argv: tuple[str, ...]) -> Completed:
    del argv
    raise KeyboardInterrupt


def test_a_ctrl_c_during_step_0s_backup_says_what_is_still_frozen(drill: Drill) -> None:
    drill.stand.answer('kubectl', 'get', 'jobs', reply=_interrupted)

    assert drill.run('0') == cli.STOPPED

    assert any('interrupted at backup' in line for line in drill.said)
    assert any('left frozen' in line and 'both backup schedules are suspended' in line for line in drill.said)


def test_a_stand_down_whose_patch_did_not_take_still_counts_the_schedules_suspended(drill: Drill) -> None:
    drill.stand.answer('kubectl', 'patch', 'cronjob', '{"spec":{"suspend":false}}', reply=done(exit=1, err='Forbidden'))
    drill.person.answers['paste its lines'] = [f'{DOMAINS[0]} OUT_OF_HOST_CAPACITY 0', '']

    assert drill.run('0') == cli.STOPPED

    assert drill.record().fact(steps.SUSPENDED) is True


def test_a_piped_field_that_is_not_secret_is_handed_the_files_contents(
    drill: Drill, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A plain field's flag takes the value itself, so the drill reads the file rather than naming it."""
    note = devices.Field('note', 'note', 'a note of several lines', secret=False, multiline=True)
    value = sealed.SealedValue(
        name='a-plain-row',
        namespace='probe',
        keys=('note',),
        stack='apps',
        sealed_by=sealed.SealingCommand('a-plain-row', 'record'),
    )
    plain = devices.SealedRecord(
        member='a-plain-row',
        register='a plain row',
        title='a plain row',
        console='the pipe',
        fields=(note,),
        sealed=value,
    )
    monkeypatch.setattr(devices, 'SEALED_RECORDS', {**devices.SEALED_RECORDS, plain.member: plain})
    monkeypatch.setattr(sealed, 'VALUES', {**sealed.VALUES, value.name: value})
    _step_9_estate(drill)
    held = tmp_path / 'note.txt'
    _ = held.write_text('line one\nline two\n')
    # The row added last to the census is the sitting's last piped command.
    drill.person.answers['piped in'][-1] = str(held)

    assert drill.run('9') == cli.DONE

    (ran,) = drill.stand.ran('credentials', 'derived', 'a-plain-row', 'record')
    assert ran.argv[-2:] == (note.flag, 'line one\nline two')


# --------------------------------------------------------------------------
# The tools a step starts.
# --------------------------------------------------------------------------


def _whole_step_3(drill: Drill) -> None:
    _instances(drill)
    drill.person.answers['Type the names'] = [' '.join(INSTANCES)]


#: Each step a case runs whole, from the estate it meets.
WHOLE_STEPS: dict[str, Callable[[Drill], None]] = {'0': lambda _: None, '3': _whole_step_3, '9': _step_9_estate}


@pytest.mark.parametrize('step', list(WHOLE_STEPS))
def test_every_command_a_whole_step_starts_is_a_pin_a_console_script_or_the_hosts(drill: Drill, step: str) -> None:
    WHOLE_STEPS[step](drill)
    pinned = set(tomllib.loads((ROOT / 'mise.toml').read_text())['tools'])
    scripts = set(tomllib.loads((ROOT / 'pyproject.toml').read_text())['project']['scripts'])

    assert drill.run(step) == cli.DONE

    started = {call.argv[0] for call in drill.stand.calls}
    assert started - pinned - scripts - tools.HOST_TOOLS == set()
