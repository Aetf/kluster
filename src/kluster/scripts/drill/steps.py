"""operations.md §3.5's steps, each as the commands that run it, and the pass readings after them.

What each step changes, why, what it reads and how a reading is judged, and
what a failure leaves, is §3.5's; a part here cites the step it implements
and says no more than its code needs. The step headings of §3.5 and `STEPS`
are one list, held so by `tests/test_drill.py`.

**A step is parts run in order** (`Part`), and the first that does not pass
ends it, its verdict the step's. `run.json` records each part that passed, so
`drill rebuild` resumes a step at its first part that did not, while
`drill rebuild <step>` runs the step from its first part again. A part that
runs a command more than once -- the sitting -- records each command it
finished, and resumes at the first it did not.

**Every step first checks that the working copy holds the forge's `main`**
(`manual_run.require_main`), since a rebuild spans an hour and merges move
`main` during it; a working copy that does not is refused before any command
of the step runs. The termination and the sitting check it again as they
start, since a prompt or a wait stands between them and their step's start.
A step named after step 0 runs only once step 0 has passed.
"""

from __future__ import annotations

import base64
import datetime as dt
import json
import shlex
import shutil
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, cast

from kluster import conventions
from kluster.conventions import backup, schedule, sealed
from kluster.lib import versions
from kluster.lib.monitoring.vmsingle import vmsingle_workload
from kluster.scripts import manual_run
from kluster.scripts.credentials import devices
from kluster.scripts.drill import capacity, chain, readings, record, scope
from kluster.scripts.drill.readings import NoReading, Reading
from kluster.scripts.drill.record import Record, Verdict, now
from kluster.scripts.drill.tools import Completed, Mode, Session
from kluster.scripts.perf import cluster
from kluster.scripts.update_crds import sources

PASS, FAIL, NO_READING = Verdict.PASS, Verdict.FAIL, Verdict.NO_READING

#: The question a step asks, and the answer typed; None where input ended.
type Ask = Callable[[str], str | None]

PHYSICAL = conventions.PHYSICAL
#: The stacks that read the cluster and are previewed around the rebuild.
DNS, K8S_BASE, APPS = 'dns', 'k8s-base', 'apps'

#: Where the peak recorder appends, in the run's directory (operations.md §8.5).
PEAKS = record.DIRECTORY / 'peaks'
#: The cluster's CA, written for the fronts item's IPv4 request.
CLUSTER_CA = record.DIRECTORY / 'kluster-ca.pem'

#: The Job step 0 creates from the history store's backup CronJob.
DRILL_BACKUP_JOB = 'drill-history'
HISTORY_BACKUP = schedule.METRICS_BACKUPS[conventions.HISTORY_STORE]

#: How `gateway-health` is run, in the form physical/gateway.md §1.6 gives.
GATEWAY_HEALTH = ('ssh', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', 'gw', '/data/custom/bin/gateway-health')

#: The label Talos gives every control plane.
CONTROL_PLANE_LABEL = 'node-role.kubernetes.io/control-plane'

#: The share of etcd's write-ahead-log syncs that finish within the histogram
#: bucket `FSYNC_BUCKET`, as a percentage: declarative/physical.md §6's
#: reading of cluster/nodes.md §1's criterion.
FSYNC_SHARE = 99
FSYNC_BUCKET = '0.008'


@dataclass
class Context:
    """What every part of a step reads and acts through."""

    checkout: Path
    #: The run's `run.json`.
    record: Record
    session: Session
    ask: Ask
    say: Callable[[str], None]
    clock: Callable[[], dt.datetime] = now
    #: The check that the working copy holds the forge's `main`, which a
    #: step makes before its first command and again before an act that
    #: writes from the working copy.
    require_main: Callable[[Path], None] = manual_run.require_main
    #: The cluster's client configurations, where the operator keeps them.
    kubeconfig: Path = field(default_factory=lambda: cluster.KUBECONFIG.expanduser())
    talosconfig: Path = field(default_factory=lambda: cluster.TALOSCONFIG.expanduser())

    def note(self, text: str) -> None:
        """Say `text` to the operator, and write it to the step's transcript."""
        self.say(text)
        self.session.transcript.line(text)

    def answer(self, question: str) -> str | None:
        """Ask the operator, writing the question and the answer to the transcript."""
        self.session.transcript.line(f'? {question}')
        got = self.ask(question)
        self.session.transcript.line(f'> {got if got is not None else "(no answer)"}')
        return got

    def run(
        self, argv: Sequence[str], *, mode: Mode = Mode.CAPTURED, secret: bool = False, quiet: bool = False
    ) -> Completed:
        self.say(f'running: {" ".join(argv)}')
        return self.session.run(argv, mode=mode, secret=secret, quiet=quiet)

    def pulumi(
        self, stack: str, *args: str, mode: Mode = Mode.CAPTURED, secret: bool = False, quiet: bool = False
    ) -> Completed:
        argv = ['pulumi', *args, '--stack', stack]
        self.say(f'running: {" ".join(argv)}')
        return self.session.run(argv, stack=stack, mode=mode, secret=secret, quiet=quiet)

    def kubectl(self, *args: str, mode: Mode = Mode.CAPTURED) -> Completed:
        return self.run(['kubectl', '--kubeconfig', str(self.kubeconfig), *args], mode=mode)

    def talosctl(self, *args: str) -> Completed:
        return self.run(['talosctl', '--talosconfig', str(self.talosconfig), *args])

    def gh(self, *args: str, mode: Mode = Mode.CAPTURED) -> Completed:
        return self.run(['gh', *args], mode=mode)

    # -- reads every step shares -----------------------------------------------

    def urns(self) -> list[str] | NoReading:
        listed = self.pulumi(PHYSICAL, 'stack', '--show-urns')
        if not listed.ok:
            return readings.failed(listed, "the list of physical's URNs")
        return scope.urns(listed.out)

    def export(self) -> Completed:
        return self.pulumi(PHYSICAL, 'stack', 'export', quiet=True)

    def output(self, name: str, *, json_form: bool = False) -> Completed:
        return self.pulumi(PHYSICAL, 'stack', 'output', name, *(['--json'] if json_form else []))

    def node_addresses(self) -> dict[str, str] | NoReading:
        """Each cloud node's VCN address, from `physical`'s `node_private_ips`."""
        done = self.output(conventions.PHYSICAL_OUTPUTS.node_private_ips, json_form=True)
        if not done.ok:
            return readings.failed(done, 'the read of node_private_ips')
        try:
            found = cast('object', json.loads(done.out or 'null'))
        except ValueError:
            return NoReading('what node_private_ips printed is not JSON')
        if not isinstance(found, dict) or not found:
            return NoReading('node_private_ips holds no address')
        return {str(name): str(address) for name, address in cast('dict[str, object]', found).items()}

    def sealing(self) -> Completed:
        return self.run(
            [
                'kubeseal',
                '--kubeconfig',
                str(self.kubeconfig),
                '--fetch-cert',
                '--controller-namespace',
                conventions.SEALING_NAMESPACE,
                '--controller-name',
                conventions.SEALING_CONTROLLER,
            ]
        )


@dataclass(frozen=True)
class Part:
    """One act of a step, with the verdict it ends on."""

    name: str
    act: Callable[[Context], Verdict]


@dataclass(frozen=True)
class Step:
    """One step of §3.5, by its number, or the pass readings, by `PASS_READINGS_KEY`."""

    key: str
    title: str
    parts: tuple[Part, ...]
    #: What the step does once it has stopped short of a pass.
    on_stop: Callable[[Context], None] | None = None


def _verdict(ok: bool) -> Verdict:
    return PASS if ok else FAIL


def _at_main(ctx: Context) -> bool:
    """The working copy checked against the forge's `main` again, ahead of an act that writes from it."""
    try:
        ctx.require_main(ctx.checkout)
    except manual_run.CheckRefused as exc:
        ctx.note(f'refused: {exc}')
        return False
    return True


def _json(ctx: Context, done: Completed, what: str) -> object | None:
    """What `done` printed, as JSON; None, said, where the command failed or printed something else."""
    if not done.ok:
        ctx.note(readings.failed(done, f'the read of {what}').why)
        return None
    try:
        return cast('object', json.loads(done.out))
    except ValueError:
        ctx.note(f'what the read of {what} printed is not JSON')
        return None


def _items(ctx: Context, done: Completed, what: str) -> list[dict[str, Any]] | None:
    """The `items` of a `kubectl get -o json` list; None where there is no such list."""
    parsed = _json(ctx, done, what)
    if not isinstance(parsed, dict):
        return None
    items = cast('dict[str, Any]', parsed).get('items')
    return (
        [cast('dict[str, Any]', item) for item in cast('list[object]', items) if isinstance(item, dict)]
        if isinstance(items, list)
        else None
    )


def _name(item: dict[str, Any]) -> str:
    return str(cast('dict[str, Any]', item.get('metadata') or {}).get('name'))


# --------------------------------------------------------------------------
# The baselines, as step 0 takes them and later steps read them again.
# --------------------------------------------------------------------------


def _protected(ctx: Context) -> Reading[frozenset[str]]:
    return readings.protected(ctx.export())


def _outside(ctx: Context) -> Reading[str]:
    # Only the digest leaves the export: the transcript keeps its exit alone.
    export = ctx.pulumi(PHYSICAL, 'stack', 'export', '--show-secrets', secret=True)
    return readings.outside(export, scope.SCOPE)


def _anchors(ctx: Context) -> Reading[tuple[readings.Anchor, ...]]:
    return tuple(
        readings.anchor(name, kind, ctx.run(['doggo', '--short', name, kind, readings.RESOLVER]))
        for name, kind in readings.ANCHORS
    )


def _ids(ctx: Context, volume: str) -> Reading[readings.Ids]:
    addresses = ctx.node_addresses()
    if isinstance(addresses, NoReading):
        return addresses
    node = conventions.NODE_VOLUMES[volume].attached_node
    return readings.ids(volume, ctx.talosctl('-n', addresses[node], 'get', 'discoveredvolumes', '-o', 'json'))


def _metrics(ctx: Context) -> Reading[readings.Metrics]:
    return readings.metrics(
        ctx.kubectl('get', '--raw', cluster.history('/metrics')),
        ctx.kubectl('get', '--raw', cluster.history(readings.EARLIEST_QUERY)),
    )


def _sealing(ctx: Context) -> Reading[str]:
    return readings.sealing(ctx.sealing())


def _outputs(ctx: Context) -> Reading[dict[str, str]]:
    return readings.outputs({name: ctx.output(name) for name in readings.OUTPUTS})


def _baselines(ctx: Context) -> list[tuple[readings.Kind[Any], Callable[[Context], Reading[Any]]]]:
    return [
        (readings.PROTECTED, _protected),
        (readings.OUTSIDE, _outside),
        (readings.ANCHOR_READINGS, _anchors),
        *((readings.ids_kind(volume), lambda c, v=volume: _ids(c, v)) for volume in readings.volumes()),
        (readings.SEALING, _sealing),
        (readings.METRICS, _metrics),
        (readings.OUTPUT_READINGS, _outputs),
    ]


def baseline[T](ctx: Context, kind: readings.Kind[T]) -> Reading[T]:
    return kind.restore(ctx.record.baseline(kind.name))


def unwhole_baselines(ctx: Context) -> list[str]:
    """Every baseline that is not whole, each with why: what refuses the go."""
    return [
        f'{kind.name}: {why}'
        for kind, _ in _baselines(ctx)
        if (why := kind.why_not_whole(baseline(ctx, kind))) is not None
    ]


def _compare[T](ctx: Context, kind: readings.Kind[T], reading: Reading[T]) -> bool:
    kept = baseline(ctx, kind)
    ctx.note(f'baseline  {kind.shown(kept)}')
    ctx.note(f'now       {kind.shown(reading)}')
    return readings.same(kept, reading)


# --------------------------------------------------------------------------
# Readings several steps take.
# --------------------------------------------------------------------------


def gateway_health(ctx: Context) -> Verdict:
    """`gateway-health`, clean when it prints nothing and exits 0; asked of the operator where it cannot run here."""
    done = ctx.run(list(GATEWAY_HEALTH))
    if done.exit == 0 and not done.out.strip():
        ctx.note('gateway-health is clean')
        return PASS
    if done.exit == 1 and done.out.strip():
        ctx.note(f'gateway-health reports findings:\n{done.out.rstrip()}')
        return FAIL
    got = ctx.answer(
        f'gateway-health did not run from here ({done.said}). Run it as physical/gateway.md §1.6 gives, and type '
        '`clean` if it printed nothing and exited 0:'
    )
    return _verdict(got is not None and got.strip() == 'clean')


def _preview(ctx: Context, stack: str) -> bool:
    """A refreshed preview that plans no change: `physical` with its program run, as every preview of it is."""
    program = ['--run-program'] if stack == PHYSICAL else []
    return ctx.pulumi(stack, 'preview', '--refresh', *program, '--expect-no-changes', mode=Mode.SHOWN).ok


def _take_capacity(ctx: Context) -> capacity.Capacity | None:
    ctx.note(
        "Take operations.md §3.4's capacity reading in the OCI console's Cloud Shell, at "
        f'{conventions.NODE_OCPUS} OCPUs and {conventions.NODE_MEMORY_GB} GB, for every AD of the region.'
    )
    lines: list[str] = []
    while (line := ctx.answer('paste its lines, one AD each, then an empty line:')) not in (None, ''):
        lines.append(line)
    try:
        taken = capacity.parse(lines, ctx.clock())
    except capacity.Unreadable as exc:
        ctx.note(f'the reading is not read: {exc}')
        return None
    ctx.record.keep_fact('capacity', taken.dump())
    return taken


def _recorded_capacity(ctx: Context) -> capacity.Capacity | None:
    kept = ctx.record.fact('capacity')
    return capacity.Capacity.load(cast('dict[str, object]', kept)) if isinstance(kept, dict) else None


def _stand_down(ctx: Context) -> bool:
    """A run that does not go: `deploy.yml` enabled again, and both backup schedules resumed; False where one did not take."""
    ctx.note('the run does not go: enabling deploy.yml and resuming the backup schedules')
    # The next run starts at the freeze again, rather than at a go over an
    # unfrozen estate.
    ctx.record.forget(STEPS[0].key)
    enabled = ctx.gh('workflow', 'enable', chain.WORKFLOW, '--repo', chain.REPOSITORY)
    resumed = [
        ctx.kubectl('-n', job.namespace, 'patch', 'cronjob', job.cronjob, '-p', '{"spec":{"suspend":false}}')
        for job in schedule.METRICS_BACKUPS.values()
    ]
    # Recorded as resumed only once every schedule took its patch, so that a
    # later stop still names the schedules a failed patch left suspended.
    if all(done.ok for done in resumed):
        ctx.record.keep_fact(SUSPENDED, False)
    if failed := [done for done in (enabled, *resumed) if not done.ok]:
        ctx.note(
            'still frozen, since these did not take; run each again by hand: '
            + '; '.join(f'`{shlex.join(done.argv)}` ({done.said})' for done in failed)
        )
        return False
    ctx.note('deploy.yml is enabled again, and both backup schedules resumed')
    return True


#: The fact that step 0 has suspended the backup schedules.
SUSPENDED = 'schedules suspended'

#: The answer that stands a stopped step 0 down.
STAND_DOWN = 'stand down'


def _still_frozen(ctx: Context) -> None:
    """Where step 0 stops after its freeze: say what is still frozen, and offer to stand it down."""
    if 'freeze' not in ctx.record.passed_parts(STEPS[0].key):
        return
    frozen = ['deploy.yml is disabled']
    if ctx.record.fact(SUSPENDED):
        frozen.append('both backup schedules are suspended')
    got = ctx.answer(
        f'{" and ".join(frozen)}. Type `{STAND_DOWN}` to undo that now, or leave it and run `drill rebuild` to go on '
        'from where step 0 stopped:'
    )
    if got == STAND_DOWN:
        _ = _stand_down(ctx)
    else:
        ctx.note(f'left frozen: {"; ".join(frozen)}')


# --------------------------------------------------------------------------
# Step 0: the baseline, and the go.
# --------------------------------------------------------------------------


def _freeze(ctx: Context) -> Verdict:
    return _verdict(ctx.gh('workflow', 'disable', chain.WORKFLOW, '--repo', chain.REPOSITORY).ok)


def _converged(ctx: Context) -> Verdict:
    # Every preview runs, so that a diff in one does not hide another's.
    clean = [_preview(ctx, stack) for stack in (PHYSICAL, DNS, K8S_BASE, APPS)]
    return _verdict(all(clean))


def _take_baselines(ctx: Context) -> Verdict:
    for kind, read in _baselines(ctx):
        reading = read(ctx)
        ctx.record.keep_baseline(kind.name, kind.keep(reading))
        ctx.note(kind.shown(reading))
    unwhole = unwhole_baselines(ctx)
    for each in unwhole:
        ctx.note(f'not whole, taken again: {each}')
    return NO_READING if unwhole else PASS


def _ended(ctx: Context, namespace: str, job: str, bound: dt.timedelta) -> str | None:
    """Wait for `job` to complete within `bound`; then its terminal condition, `Complete` or `Failed`, or None.

    One condition is waited on, since a finished Job holds two that are true
    (`SuccessCriteriaMet` and `Complete`, or `FailureTarget` and `Failed`), and
    an expression matching both is an error to `kubectl wait`. A Job that
    fails is read once the bound has elapsed.
    """
    ctx.note(f'waiting for the job {job} to complete (at most {bound}; a job that fails is read then)')
    timeout = f'--timeout={int(bound.total_seconds())}s'
    if ctx.kubectl('-n', namespace, 'wait', f'job/{job}', '--for=condition=Complete', timeout, mode=Mode.SHOWN).ok:
        return 'Complete'
    read = ctx.kubectl('-n', namespace, 'get', 'job', job, '-o', 'json')
    parsed = _json(ctx, read, f'the job {job}')
    job_read = cast('dict[str, Any]', parsed) if isinstance(parsed, dict) else {}
    status = cast('dict[str, Any]', job_read.get('status') or {})
    conditions = cast('list[dict[str, Any]]', status.get('conditions') or [])
    failed = any(each.get('type') == 'Failed' and each.get('status') == 'True' for each in conditions)
    ctx.note(f'the job {job} {"failed" if failed else "did not end within its bound"}')
    return 'Failed' if failed else None


def _backup(ctx: Context) -> Verdict:
    namespace = HISTORY_BACKUP.namespace
    bound = HISTORY_BACKUP.deadline + dt.timedelta(minutes=5)
    ctx.record.keep_fact(SUSPENDED, True)
    for job in schedule.METRICS_BACKUPS.values():
        if not ctx.kubectl('-n', job.namespace, 'patch', 'cronjob', job.cronjob, '-p', '{"spec":{"suspend":true}}').ok:
            return FAIL
    listed = ctx.kubectl('-n', namespace, 'get', 'jobs', '-o', 'json')
    if not listed.ok:
        return FAIL
    if (items := _items(ctx, listed, 'the jobs')) is None:
        return NO_READING
    for job in (_name(item) for item in items if cast('dict[str, Any]', item.get('status') or {}).get('active')):
        if _ended(ctx, namespace, job, bound) is None:
            return FAIL
    _ = ctx.kubectl('-n', namespace, 'delete', 'job', DRILL_BACKUP_JOB, '--ignore-not-found')
    if not ctx.kubectl(
        '-n', namespace, 'create', 'job', DRILL_BACKUP_JOB, f'--from=cronjob/{HISTORY_BACKUP.cronjob}'
    ).ok:
        return FAIL
    if _ended(ctx, namespace, DRILL_BACKUP_JOB, bound) != 'Complete':
        return FAIL
    succeeded = ctx.kubectl('-n', namespace, 'get', 'job', DRILL_BACKUP_JOB, '-o', 'jsonpath={.status.succeeded}')
    return _verdict(succeeded.ok and succeeded.out.strip() == '1')


def _domains(ctx: Context) -> frozenset[str] | None:
    """The availability domain of every cloud node `physical`'s state holds an instance of."""
    export = ctx.export()
    listed = scope.resources(export.out) if export.ok else None
    if listed is None:
        ctx.note(f'the availability domains of the instances are not read: {export.said}')
        return None
    return scope.domains(listed, INSTANCE, [_display(node) for node in conventions.CLOUD_NODES])


def _capacity(ctx: Context) -> Verdict:
    if (required := _domains(ctx)) is None:
        return NO_READING
    taken = _take_capacity(ctx)
    refusal = 'no reading was pasted' if taken is None else taken.refusal(ctx.clock(), required)
    if refusal is None:
        return PASS
    ctx.note(f'the capacity reading does not pass: {refusal}; the run waits for another day')
    _ = _stand_down(ctx)
    return FAIL


def _go(ctx: Context) -> Verdict:
    for kind, _ in _baselines(ctx):
        ctx.note(kind.shown(baseline(ctx, kind)))
    if unwhole := unwhole_baselines(ctx):
        ctx.note(
            f'the go is refused while a baseline is not whole: {"; ".join(unwhole)}. '
            f'`drill rebuild {STEPS[0].key}` takes them again'
        )
        return NO_READING
    if ctx.answer('Every baseline is whole. Type `go` to go, anything else to stand down:') != 'go':
        _ = _stand_down(ctx)
        return FAIL
    ctx.note(
        'Start the peak recorder now, in a terminal of its own at the root of the checkout, and leave it running: '
        f'`mise x uv -- uv run perf record {PEAKS}`'
    )
    return PASS


# --------------------------------------------------------------------------
# Steps 1 and 2: the day-1 chain leaves state, and the worker's disk loses its protection.
# --------------------------------------------------------------------------


def _targets(ctx: Context, targets: scope.Targets, what: str) -> list[str] | None:
    """The URNs `targets` selects, printed; None where the list is not the count its step holds it to."""
    found = ctx.urns()
    if isinstance(found, NoReading):
        ctx.note(found.why)
        return None
    selected = targets.of(found)
    ctx.note('\n'.join(selected) or '(none)')
    if len(selected) != targets.count:
        ctx.note(f'{what}: the list holds {len(selected)}, not {targets.count}; nothing is run')
        return None
    return selected


def _day1(ctx: Context) -> Verdict:
    selected = _targets(ctx, scope.DAY1, 'the day-1 chain')
    if selected is None:
        return FAIL
    targets = [f'--target={urn}' for urn in selected]
    return _verdict(ctx.pulumi(PHYSICAL, 'destroy', '--run-program', *targets, mode=Mode.ATTACHED).ok)


def _unprotect(ctx: Context) -> Verdict:
    selected = _targets(ctx, scope.DISK, "the worker's disk")
    if selected is None:
        return FAIL
    return _verdict(ctx.pulumi(PHYSICAL, 'state', 'unprotect', *selected, mode=Mode.ATTACHED).ok)


# --------------------------------------------------------------------------
# Step 3: the loss.
# --------------------------------------------------------------------------

INSTANCE = 'oci:Core/instance:Instance'
PUBLIC_IP = 'oci:Core/publicIp:PublicIp'
VOLUME = 'oci:Core/volume:Volume'
#: The dedicated VIP's reserved public address, by its display name.
VIP = f'{conventions.CLUSTER_NAME}-vip1'


def _display(name: str) -> str:
    return f'{conventions.CLUSTER_NAME}-{name}'


def _capacity_refusal(ctx: Context) -> str | None:
    """Why the recorded capacity reading does not license a termination now, or None where it does."""
    if (required := _domains(ctx)) is None:
        return 'the availability domains of the instances are not read'
    kept = _recorded_capacity(ctx)
    return 'no reading is recorded' if kept is None else kept.refusal(ctx.clock(), required)


def _fresh_capacity(ctx: Context) -> Verdict:
    """A capacity reading the termination can stand on, taken again where the recorded one does not pass."""
    if (refusal := _capacity_refusal(ctx)) is None:
        return PASS
    ctx.note(f'the recorded capacity reading is refused: {refusal}; a fresh one is taken')
    _ = _take_capacity(ctx)
    if (refusal := _capacity_refusal(ctx)) is not None:
        ctx.note(f'nothing is terminated: {refusal}')
        return FAIL
    return PASS


def _one_each(ctx: Context, export: Completed, kind: str, names: Sequence[str]) -> list[str] | None:
    if not export.ok:
        ctx.note(readings.failed(export, 'the export of physical').why)
        return None
    if (listed := scope.resources(export.out)) is None:
        ctx.note(readings.NOT_AN_EXPORT.why)
        return None
    found = {name: scope.ocid(listed, kind, name) for name in names}
    if any(len(ids) != 1 for ids in found.values()):
        ctx.note('; '.join(f'{name}: {len(ids)} found, not one' for name, ids in found.items() if len(ids) != 1))
        return None
    return [ids[0] for ids in found.values()]


def _terminate(ctx: Context) -> Verdict:
    names = [_display(node) for node in conventions.CLOUD_NODES]
    instances = _one_each(ctx, ctx.export(), INSTANCE, names)
    if instances is None:
        return FAIL
    ctx.note('\n'.join(f'{name} {ocid}' for name, ocid in zip(names, instances, strict=True)))
    got = ctx.answer(f'From here the way is forward only. Type the names to terminate them: {" ".join(names)}')
    if got is None or got.split() != names:
        ctx.note('nothing is terminated')
        return FAIL
    # What licenses the termination is judged here, as it starts: a part that
    # passed an hour ago, or a prompt left waiting, licenses nothing.
    if (refusal := _capacity_refusal(ctx)) is not None:
        ctx.note(f'nothing is terminated: {refusal}. `drill rebuild 3` takes a fresh reading')
        return FAIL
    if not _at_main(ctx):
        ctx.note('nothing is terminated')
        return FAIL
    return _verdict(ctx.run(['credentials', 'derived', 'oci-physical', 'terminate', *instances], mode=Mode.ATTACHED).ok)


def _left(ctx: Context) -> Verdict:
    volumes = [_display(volume) for volume in readings.volumes()]
    export = ctx.export()
    vip = _one_each(ctx, export, PUBLIC_IP, [VIP])
    held = _one_each(ctx, export, VOLUME, volumes)
    if vip is None or held is None:
        return FAIL
    read = ctx.run(['credentials', 'derived', 'oci-physical', 'read', *vip, *held])
    if not read.ok:
        return FAIL
    lines = {line.split()[2]: line.split() for line in read.out.splitlines() if len(line.split()) >= 4}
    reserved = 'lifetime=RESERVED' in lines.get(vip[0], []) and 'assigned=-' in lines.get(vip[0], [])
    detached = all(
        lines.get(ocid, [''] * 4)[3] == 'AVAILABLE' and 'attached-to=-' in lines.get(ocid, []) for ocid in held
    )
    ctx.note(f'{VIP} reserved and unassigned: {reserved}; every node volume available and detached: {detached}')
    return _verdict(reserved and detached)


# --------------------------------------------------------------------------
# Step 4: the state records the loss.
# --------------------------------------------------------------------------


def _backend_sets(ctx: Context) -> int | None:
    found = ctx.urns()
    if isinstance(found, NoReading):
        ctx.note(found.why)
        return None
    return sum(1 for urn in found if scope.BACKEND_SETS.search(urn))


def _refresh_loss(ctx: Context) -> Verdict:
    sets = _backend_sets(ctx)
    if not sets:
        ctx.note('no backend set is in state; nothing is run')
        return FAIL
    selected = _targets(ctx, scope.loss(sets), 'the loss')
    if selected is None:
        return FAIL
    targets = [f'--target={urn}' for urn in selected]
    return _verdict(ctx.pulumi(PHYSICAL, 'refresh', '--run-program', *targets, mode=Mode.ATTACHED).ok)


def _loss_recorded(ctx: Context) -> Verdict:
    found = ctx.urns()
    if isinstance(found, NoReading):
        ctx.note(found.why)
        return NO_READING
    stayed_behind = [urn for urn in found if scope.LEFT_STATE.search(urn)]
    kept = [urn for urn in found if scope.STAYED.search(urn)]
    sets = sum(1 for urn in found if scope.BACKEND_SETS.search(urn))
    expected = 1 + len(conventions.CLOUD_NODES) * sets
    if stayed_behind:
        ctx.note(
            'still in state, where the refresh drops it: '
            + ', '.join(stayed_behind)
            + '. An attachment that stays takes the fallback of declarative/physical.md §6.'
        )
    if len(kept) != expected:
        ctx.note(f'the reserved IP and the backends: {len(kept)} in state, not {expected}')
    export = ctx.export()
    listed = scope.resources(export.out) if export.ok else None
    if listed is None:
        ctx.note(f"the reserved IP's private_ip_id is not read: {export.said}")
        return NO_READING
    reserved = [
        cast('dict[str, Any]', resource.get('outputs') or {})
        for resource in listed
        if resource.get('type') == PUBLIC_IP and scope.displayed(resource) == VIP
    ]
    unassigned = len(reserved) == 1 and not reserved[0].get('privateIpId')
    ctx.note(
        f"{VIP}'s private_ip_id: {reserved[0].get('privateIpId') if len(reserved) == 1 else f'{len(reserved)} found'}"
    )
    return _verdict(not stayed_behind and len(kept) == expected and unassigned)


# --------------------------------------------------------------------------
# Steps 5 and 6: the rebuild, and `physical` converges.
# --------------------------------------------------------------------------


def _rebuild(ctx: Context) -> Verdict:
    selected = _targets(ctx, scope.WORKER, "the worker's image and disk")
    if selected is None:
        return FAIL
    replace = [f'--replace={urn}' for urn in selected]
    if not ctx.pulumi(PHYSICAL, 'preview', '--diff', *replace, mode=Mode.SHOWN).ok:
        return FAIL
    planned = ctx.pulumi(PHYSICAL, 'preview', '--json', *replace, quiet=True)
    if not planned.ok:
        ctx.note(readings.failed(planned, 'the scope check').why)
        return NO_READING
    if (beyond := scope.outside_scope(planned.out)) is None:
        ctx.note('what the preview printed is not a plan: the scope check is no reading')
        return NO_READING
    ctx.note('\n'.join([str(len(beyond)), *beyond]))
    checks = cast('list[object]', ctx.record.fact('scope checks') or [])
    ctx.record.keep_fact('scope checks', [*checks, beyond])
    if beyond:
        ctx.note('the plan steps outside the scope: nothing is applied until it has been read')
        return FAIL
    return _verdict(ctx.pulumi(PHYSICAL, 'up', *replace, mode=Mode.ATTACHED).ok)


def _ephemeral(ctx: Context) -> Verdict:
    worker = str(conventions.HOMELAB_NODE_IPV4)
    done = ctx.talosctl('-n', worker, 'get', 'volumestatus', 'EPHEMERAL')
    ctx.note(done.out.rstrip() or done.said)
    got = ctx.answer(
        f"Does EPHEMERAL read {conventions.HOMELAB_DISK_GIB} GiB less the image's partitions and STATE, "
        'with no reboot of the worker? [y/N]'
    )
    return _verdict(done.ok and got is not None and got.strip().lower() in {'y', 'yes'})


def _restart_recorder(ctx: Context) -> Verdict:
    ctx.note(
        'Stop the peak recorder with Ctrl-C and start it again with the same command, so that it reads the new '
        f'nodes: `mise x uv -- uv run perf record {PEAKS}`'
    )
    return _verdict(ctx.answer('press Enter once it runs again') is not None)


def _physical_converges(ctx: Context) -> Verdict:
    # A further `up` reports no changes: it fails where it would make one.
    if not ctx.pulumi(PHYSICAL, 'up', '--expect-no-changes', mode=Mode.ATTACHED).ok:
        return FAIL
    return _verdict(_preview(ctx, PHYSICAL))


def _nothing_else_moved(ctx: Context) -> Verdict:
    kept = _compare(ctx, readings.PROTECTED, _protected(ctx))
    digest = _compare(ctx, readings.OUTSIDE, _outside(ctx))
    return _verdict(kept and digest)


# --------------------------------------------------------------------------
# Step 7: the readings that need no network plugin.
# --------------------------------------------------------------------------


def _write_secret(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.touch(mode=0o600)
    path.chmod(0o600)
    _ = path.write_text(text)


def _clients(ctx: Context) -> Verdict:
    for name, path in (
        (conventions.PHYSICAL_OUTPUTS.kubeconfig, ctx.kubeconfig),
        (conventions.PHYSICAL_OUTPUTS.talosconfig, ctx.talosconfig),
    ):
        done = ctx.pulumi(PHYSICAL, 'stack', 'output', name, '--show-secrets', secret=True)
        if not done.ok or not done.out.strip():
            ctx.note(f'the {name} output was not read: {done.said}')
            return FAIL
        _write_secret(path, done.out)
        ctx.note(f'wrote {path}')
    return PASS


def _talos_pin(ctx: Context) -> str:
    return versions.Versions(sources.read_project(ctx.checkout / 'Pulumi.yaml')).talos


def _all_nodes(ctx: Context) -> list[str] | None:
    addresses = ctx.node_addresses()
    if isinstance(addresses, NoReading):
        ctx.note(addresses.why)
        return None
    return [*addresses.values(), str(conventions.HOMELAB_NODE_IPV4)]


def _versions(ctx: Context) -> Verdict:
    nodes = _all_nodes(ctx)
    if nodes is None:
        return NO_READING
    pin = _talos_pin(ctx)
    answered = {node: ctx.talosctl('-n', node, 'version', '--short') for node in nodes}
    off = [node for node, done in answered.items() if not done.ok or pin not in done.out.partition('Server:')[2]]
    ctx.note(f'every node answers at {pin}' if not off else f'not at {pin}, or no answer: {", ".join(off)}')
    return _verdict(not off)


def _documents(ctx: Context, done: Completed, what: str) -> list[object] | None:
    """The resources a `talosctl -o json` printed; None, said, where it failed or printed something else."""
    if not done.ok:
        ctx.note(readings.failed(done, f'the read of {what}').why)
        return None
    try:
        return readings.json_stream(done.out)
    except ValueError:
        ctx.note(f'what the read of {what} printed is not JSON')
        return None


def _link(ctx: Context) -> Verdict:
    worker = str(conventions.HOMELAB_NODE_IPV4)
    done = ctx.talosctl('-n', worker, 'get', 'addresses', '-o', 'json')
    gua = ctx.output(conventions.PHYSICAL_OUTPUTS.worker_gua)
    documents = _documents(ctx, done, "the worker's addresses")
    if documents is None or not gua.ok:
        return NO_READING
    specs = [readings.specification(document) for document in documents]
    link_local = f'{conventions.HOMELAB_NODE_LINK_LOCAL}/64'
    forms = any(
        spec.get('family') == 'inet6' and spec.get('scope') == 'link' and spec.get('address') == link_local
        for spec in specs
    )
    holds = any(str(spec.get('address', '')).split('/')[0] == gua.out.strip() for spec in specs)
    ctx.note(f'the worker forms {link_local}: {forms}; it holds {gua.out.strip()}: {holds}')
    return _verdict(forms and holds)


def _samples(exposition: str) -> dict[str, float]:
    """Each series of a Prometheus exposition with no timestamp, by its name and labels; a line of anything else is not one."""
    found: dict[str, float] = {}
    for line in exposition.splitlines():
        fields = line.split()
        if len(fields) == 2 and not line.startswith('#'):
            try:
                found[fields[0]] = float(fields[1])
            except ValueError:
                continue
    return found


def _etcd(ctx: Context) -> Verdict:
    addresses = ctx.node_addresses()
    listed = ctx.kubectl('get', 'nodes', '-l', CONTROL_PLANE_LABEL, '-o', 'jsonpath={.items[*].metadata.name}')
    if isinstance(addresses, NoReading) or not listed.ok:
        return NO_READING
    names = listed.out.split()
    ok = len(names) == len(conventions.CLOUD_NODES)
    if not ok:
        ctx.note(f'{len(names)} control planes labelled, {len(conventions.CLOUD_NODES)} declared')
    within_bucket = f'etcd_disk_wal_fsync_duration_seconds_bucket{{le="{FSYNC_BUCKET}"}}'
    for name in names:
        scraped = ctx.kubectl('get', '--raw', cluster.etcd_metrics(name))
        values = _samples(scraped.out) if scraped.ok else {}
        within, count = values.get(within_bucket, 0.0), values.get('etcd_disk_wal_fsync_duration_seconds_count', 0.0)
        passes = count > 0 and 100 * within >= FSYNC_SHARE * count
        ctx.note(f'{name}: {within:.0f} of {count:.0f} syncs within 8 ms, {"pass" if passes else "FAIL"}')
        log = ctx.talosctl('-n', addresses.get(name, name), 'logs', 'etcd')
        slow = log.out.count('slow fdatasync') if log.ok else None
        ctx.note(f'{name}: slow fdatasync {slow if slow is not None else "not read"}')
        ok = ok and passes and slow == 0
    return _verdict(ok)


def _fronts(ctx: Context) -> Verdict:
    authority = ctx.kubectl(
        'config', 'view', '--raw', '-o', 'jsonpath={.clusters[0].cluster.certificate-authority-data}'
    )
    v4 = ctx.output(conventions.PHYSICAL_OUTPUTS.cluster_endpoint)
    v6 = ctx.output(conventions.PHYSICAL_OUTPUTS.cluster_endpoint_v6)
    if not (authority.ok and v4.ok and v6.ok):
        return NO_READING
    _ = (ctx.checkout / CLUSTER_CA).write_bytes(base64.b64decode(authority.out.strip()))
    common = [
        'curl',
        '--noproxy',
        '*',
        '--connect-timeout',
        '10',
        '--max-time',
        '20',
        '-sS',
        '-o',
        '/dev/null',
        '-w',
        '%{http_code}',
    ]
    answers = [
        ctx.run([*common, '--cacert', str(ctx.checkout / CLUSTER_CA), f'https://{v4.out.strip()}:6443/version']),
        ctx.run([*common, '-k', f'https://[{v6.out.strip()}]:6443/version']),
    ]
    for done in answers:
        ctx.note(f'{done.argv[-1]}: {done.out.strip() or "000"} exit {done.exit} {done.err.strip()}')
    return _verdict(all(done.ok and done.out.strip() == '401' for done in answers))


def _volume_ids(ctx: Context) -> Verdict:
    ok = True
    for volume in readings.volumes():
        kind = readings.ids_kind(volume)
        rebuilt = _ids(ctx, volume)
        ctx.record.keep_fact(f'{kind.name} rebuilt', kind.keep(rebuilt))
        passes = readings.ids_pass(baseline(ctx, kind), rebuilt)
        ctx.note(f'baseline  {kind.shown(baseline(ctx, kind))}')
        ctx.note(f'rebuilt   {kind.shown(rebuilt)}')
        ctx.note(f'ids {volume} {"pass" if passes else "fail"}')
        ok = ok and passes
    return _verdict(ok)


def _local_storage(ctx: Context) -> Verdict:
    nodes = _all_nodes(ctx)
    if nodes is None:
        return NO_READING
    done = ctx.talosctl('-n', ','.join(nodes), 'get', 'volumestatus', 'u-storage', '-o', 'json')
    if (documents := _documents(ctx, done, 'u-storage')) is None:
        return NO_READING
    phases: dict[str, str] = {}
    for document in documents:
        spec = readings.specification(document)
        node = cast('dict[str, object]', document).get('node', '') if isinstance(document, dict) else ''
        phases[str(node)] = str(spec.get('phase'))
    ready = len(phases) == len(nodes) and all(phase == 'ready' for phase in phases.values())
    ctx.note(
        f'u-storage: {", ".join(f"{node} {phase}" for node, phase in sorted(phases.items())) or "no node answered"}'
    )
    return _verdict(ready)


def _node_label(ctx: Context) -> Verdict:
    ok = True
    for volume, entry in conventions.NODE_VOLUMES.items():
        done = ctx.kubectl('get', 'node', entry.attached_node, '-o', 'json')
        parsed = _json(ctx, done, f'the node {entry.attached_node}')
        node = cast('dict[str, Any]', parsed) if isinstance(parsed, dict) else {}
        labels = cast('dict[str, str]', cast('dict[str, Any]', node.get('metadata') or {}).get('labels') or {})
        held = labels.get(conventions.NODE_VOLUME_LABEL)
        ctx.note(f'{entry.attached_node}: {conventions.NODE_VOLUME_LABEL}={held}')
        ok = ok and held == volume
    return _verdict(ok)


# --------------------------------------------------------------------------
# Step 8: `k8s-base` and `apps` from empty, by a dispatched chain.
# --------------------------------------------------------------------------


def _refresh_cluster_stacks(ctx: Context) -> Verdict:
    refreshed = [ctx.pulumi(stack, 'refresh', mode=Mode.ATTACHED).ok for stack in (K8S_BASE, APPS)]
    return _verdict(all(refreshed))


def _listed(ctx: Context, event: str) -> list[chain.Run] | None:
    done = ctx.run(chain.listing(event))
    if not isinstance(parsed := _json(ctx, done, f"deploy.yml's {event} runs"), list):
        return None
    try:
        return chain.runs(cast('list[object]', parsed))
    except (KeyError, TypeError, ValueError):
        ctx.note(f"what the list of deploy.yml's {event} runs printed is not a list of runs")
        return None


def _dispatch(ctx: Context) -> Verdict:
    before = _listed(ctx, 'workflow_dispatch')
    if before is None:
        return NO_READING
    ctx.record.keep_fact('dispatched before', [run.id for run in before])
    # A new dispatch starts step 9's wait over, on the run it creates.
    ctx.record.forget(STEP_9)
    if not ctx.gh('workflow', 'enable', chain.WORKFLOW, '--repo', chain.REPOSITORY).ok:
        return FAIL
    return _verdict(ctx.gh('workflow', 'run', chain.WORKFLOW, '--repo', chain.REPOSITORY, '--ref', 'main').ok)


# --------------------------------------------------------------------------
# Step 9: the new controller answers, and one pull request seals.
# --------------------------------------------------------------------------


def _dispatched(ctx: Context) -> Verdict:
    after = _listed(ctx, 'workflow_dispatch')
    if after is None:
        return NO_READING
    before = cast('list[int]', ctx.record.fact('dispatched before') or [])
    try:
        run = chain.created(before, after)
    except chain.Ambiguous as exc:
        ctx.note(str(exc))
        return FAIL
    if run is None:
        ctx.note('the dispatch has created no run yet; `drill rebuild` reads it again')
        return NO_READING
    ctx.record.keep_fact('dispatched', {'id': run.id, 'url': run.url})
    ctx.note(f'the dispatched run: {run.url}')
    return PASS


def _controller(ctx: Context) -> Verdict:
    deployment = f'deployment/{conventions.SEALING_CONTROLLER}'
    ctx.note(f'waiting for {deployment} in {conventions.SEALING_NAMESPACE} to be created, then Available')
    waited = (
        ctx.kubectl(
            '-n', conventions.SEALING_NAMESPACE, 'wait', deployment, '--for=create', '--timeout=1h', mode=Mode.SHOWN
        ).ok
        and ctx.kubectl(
            '-n',
            conventions.SEALING_NAMESPACE,
            'wait',
            deployment,
            '--for=condition=Available',
            '--timeout=15m',
            mode=Mode.SHOWN,
        ).ok
    )
    if not waited:
        url = record.as_mapping(ctx.record.fact('dispatched')).get('url')
        ctx.note(f'a bound elapsed: read the run {url} for where the chain stands')
    return _verdict(waited)


def _certificate(ctx: Context) -> Verdict:
    served = _sealing(ctx)
    kept = baseline(ctx, readings.SEALING)
    if isinstance(served, NoReading):
        ctx.note(f'{served.why}; the part runs again')
        return FAIL
    count = readings.certificates(served)
    differs = not isinstance(kept, NoReading) and served != kept
    ctx.note(f"{count} certificate(s); the baseline's: {'no' if differs else 'yes'}")
    if count != 1 or not differs:
        ctx.note('sealing fail')
        return FAIL
    ctx.record.keep_fact('sealing rebuilt', served)
    ctx.note('sealing pass')
    return PASS


#: The operator's answers to the carry's question.
CARRIED, DECOMMISSIONED = 'carried', 'decommissioned'


def _carry(ctx: Context) -> Verdict:
    got = ctx.answer(
        "Until the legacy cluster's decommission, carry the legacy sealing key now by physical/bring-up.md stage "
        f"9's pipe, restarting the controller, then type `{CARRIED}`. Once the legacy cluster is gone, type "
        f'`{DECOMMISSIONED}`:'
    )
    if got == DECOMMISSIONED:
        return PASS
    if got != CARRIED:
        return FAIL
    deployment = f'deployment/{conventions.SEALING_CONTROLLER}'
    if not ctx.kubectl(
        '-n', conventions.SEALING_NAMESPACE, 'rollout', 'status', deployment, '--timeout=15m', mode=Mode.SHOWN
    ).ok:
        return FAIL
    served = _sealing(ctx)
    if not isinstance(served, NoReading) and served == ctx.record.fact('sealing rebuilt'):
        ctx.note("the controller serves the certificate condition 3 read: the cluster's own")
        return PASS
    ctx.note(
        'the controller does not serve the certificate condition 3 read, so it would seal under another key: '
        'nothing is sealed. Delete the carried key, restart the controller and carry it again '
        '(physical/bring-up.md §3), then run `drill rebuild`.'
    )
    return FAIL


def sitting() -> list[sealed.SealingCommand]:
    """Every command that seals a census row, each once, in the census's order."""
    return list(dict.fromkeys(value.sealed_by for value in sealed.VALUES.values()))


#: The verb of a row whose value a person supplies, by a field of its record.
RECORD = 'record'

#: The operator's answer that a piped command ran in their own terminal and exited 0.
EXITED_0 = 'exited 0'

#: What a piped command's source is: the operator's word that it ran, or the
#: flags that name a file for each piped field.
type Source = Literal['ran'] | tuple[str, ...]


def piped(command: sealed.SealingCommand) -> tuple[devices.Field, ...]:
    """The fields of `command`'s record that are piped in: several lines each, which a terminal cannot give.

    Read off `credentials`' own records, so a row whose record gains such a
    field is given a source by that alone.
    """
    found = _record_of(command)
    return tuple(each for each in found.fields if each.multiline) if found is not None else ()


def _record_of(command: sealed.SealingCommand) -> devices.Device | devices.SealedRecord | None:
    """The record a `record` command reads its fields by, looked up in the order `credentials`' dispatcher takes."""
    if command.verb != RECORD:
        return None
    if command.row in devices.DEVICES:
        return devices.DEVICES[command.row]
    return devices.SEALED_RECORDS.get(command.row)


def _source(ctx: Context, command: sealed.SealingCommand, fields: Sequence[devices.Field]) -> Source | None:
    """Where a piped command takes its fields from: a file for each, or a run in the operator's own terminal.

    A secret field's flag names a file, which the command reads; a plain
    field's takes the value itself, so the drill reads the file for it.
    """
    if (found := _record_of(command)) is not None:
        ctx.note(f'  {found.console}')
    flags: list[str] = []
    for index, each in enumerate(fields):
        question = (
            f'`{command}` takes {each.describes} piped in, which this terminal cannot give. '
            'Type the path of a file holding it'
        )
        if index == 0:
            question += f', or run the pipe above in a terminal of your own and type `{EXITED_0}` once it has'
        got = ctx.answer(f'{question}:')
        given = got.strip() if got is not None else ''
        if index == 0 and given == EXITED_0:
            return 'ran'
        path = Path(given).expanduser()
        if not given or not path.is_file():
            ctx.note(f'no file at {given!r}: `{command}` is not run')
            return None
        flags += [each.flag, str(path) if each.secret else path.read_text().strip()]
    return tuple(flags)


def _sit(ctx: Context) -> Verdict:
    # From a working copy at the forge's main (credentials.md §1 rule 6): the
    # waits before the sitting leave time for a merge to move it.
    if not _at_main(ctx):
        return FAIL
    done = list(ctx.record.progress(STEP_9))
    for command in sitting():
        if str(command) in done:
            continue
        argv = ['credentials', 'derived', command.row, command.verb]
        if fields := piped(command):
            source = _source(ctx, command, fields)
            if source is None:
                return FAIL
            if source == 'ran':
                ctx.note(f"`{command}` ran in the operator's own terminal and exited 0")
                done.append(str(command))
                ctx.record.keep_progress(STEP_9, done)
                continue
            argv += source
        if not ctx.run(argv, mode=Mode.ATTACHED).ok:
            ctx.note(f'`{command}` stopped the sitting; `drill rebuild` runs it, and those after it, again')
            return FAIL
        done.append(str(command))
        ctx.record.keep_progress(STEP_9, done)
    return PASS


def _copies(ctx: Context) -> Verdict:
    return _verdict(ctx.run(['credentials', 'derived', 'sync', '--only', 'kubeconfig'], mode=Mode.ATTACHED).ok)


def sealed_files(checkout: Path) -> list[str]:
    """What the sitting and the sync write, relative to `checkout`: every stack file a census row names, and the recipient."""
    stacks = dict.fromkeys(value.stack for value in sealed.VALUES.values())
    recipient = backup.ETCD_DRILL_RECIPIENT.resolve()
    named = recipient.relative_to(checkout.resolve()) if recipient.is_relative_to(checkout.resolve()) else recipient
    return [*(f'Pulumi.{stack}.yaml' for stack in stacks), str(named)]


def _merge(ctx: Context) -> Verdict:
    ctx.note(f'Commit, in one pull request: {", ".join(sealed_files(ctx.checkout))}')
    got = ctx.answer('Once it has merged, type its number:')
    if got is None or not got.strip().isdigit():
        return NO_READING
    viewed = ctx.gh('pr', 'view', got.strip(), '--repo', chain.REPOSITORY, '--json', 'state,mergeCommit')
    if not isinstance(parsed := _json(ctx, viewed, f'#{got.strip()}'), dict):
        return NO_READING
    view = cast('dict[str, Any]', parsed)
    merged = view.get('mergeCommit')
    if view.get('state') != 'MERGED' or not isinstance(merged, dict) or not cast('dict[str, Any]', merged).get('oid'):
        ctx.note(f'#{got.strip()} is {view.get("state")}, not merged')
        return NO_READING
    ctx.record.keep_fact('merge', str(cast('dict[str, Any]', merged)['oid']))
    return PASS


def _watched(ctx: Context, run_id: int, url: str) -> dict[str, Any] | None:
    """Watch the run until it ends, then read its attempt, conclusion and jobs; None where the read failed."""
    ctx.note(f'watching {url}')
    _ = ctx.gh('run', 'watch', str(run_id), '--repo', chain.REPOSITORY, '--exit-status', mode=Mode.ATTACHED)
    viewed = ctx.gh('run', 'view', str(run_id), '--repo', chain.REPOSITORY, '--json', 'attempt,conclusion,jobs')
    parsed = _json(ctx, viewed, url)
    return cast('dict[str, Any]', parsed) if isinstance(parsed, dict) else None


def _chain_converged(ctx: Context, url: str, view: dict[str, Any]) -> Verdict:
    if (why := chain.unconverged(json.dumps(view))) is not None:
        ctx.note(f'{url}: {why}')
        return FAIL
    ctx.note(f'{url}: {" and ".join(chain.APPLYING_JOBS)} converged in one run')
    return PASS


def _chains(ctx: Context) -> Verdict:
    dispatched = record.as_mapping(ctx.record.fact('dispatched'))
    url = str(dispatched['url'])
    if (view := _watched(ctx, int(str(dispatched['id'])), url)) is None:
        return NO_READING
    if (verdict := _chain_converged(ctx, url, view)) is not PASS:
        return verdict
    merge = str(ctx.record.fact('merge'))
    read: set[int] = set()
    while True:
        pushed = _listed(ctx, 'push')
        applying = chain.applied(merge, pushed) if pushed is not None else None
        if applying is None or applying.id in read:
            ctx.note(f'no run that applies {merge[:12]} is listed yet; `drill rebuild` reads it again')
            return NO_READING
        read.add(applying.id)
        ctx.record.keep_fact('applied', {'id': applying.id, 'url': applying.url})
        if (view := _watched(ctx, applying.id, applying.url)) is None:
            return NO_READING
        if view.get('conclusion') == chain.CANCELLED:
            ctx.note(f'{applying.url} was replaced by a later push; reading the run that replaced it')
            continue
        return _chain_converged(ctx, applying.url, view)


def _true(node: dict[str, Any], kind: str) -> bool:
    """Whether `node` reports the condition `kind` true."""
    conditions: object = cast('dict[str, Any]', node.get('status') or {}).get('conditions') or []
    return any(
        each.get('type') == kind and each.get('status') == 'True' for each in cast('list[dict[str, Any]]', conditions)
    )


def _no_disk_pressure(ctx: Context, nodes: Sequence[dict[str, Any]]) -> bool:
    pressed = [_name(node) for node in nodes if _true(node, 'DiskPressure')]
    ctx.note(f'DiskPressure: {", ".join(pressed) or "no node"}')
    return not pressed


def _gate(ctx: Context) -> Verdict:
    if (nodes := _items(ctx, ctx.kubectl('get', 'nodes', '-o', 'json'), 'the nodes')) is None:
        return NO_READING
    ready = [_name(node) for node in nodes if _true(node, 'Ready')]
    every = len(ready) == len(nodes) == len(conventions.ALL_NODES)
    pressure = _no_disk_pressure(ctx, nodes)
    proxy = ctx.kubectl('-n', 'kube-system', 'get', 'daemonset', 'kube-proxy')
    no_proxy = not proxy.ok and 'NotFound' in proxy.err
    served = _sealing(ctx)
    answers = not isinstance(served, NoReading) and readings.certificates(served) == 1
    ctx.note(
        f'every node Ready: {every}; no kube-proxy DaemonSet: {no_proxy}; the sealing certificate answering: {answers}'
    )
    memory = ctx.answer("The homelab host's available memory, read on the host with the worker running:")
    return _verdict(every and pressure and no_proxy and answers and bool(memory and memory.strip()))


def _stores(ctx: Context) -> Verdict:
    namespace = HISTORY_BACKUP.namespace
    out_of_init = [
        ctx.kubectl(
            '-n',
            namespace,
            'rollout',
            'status',
            f'deployment/{vmsingle_workload(store)}',
            '--timeout=30m',
            mode=Mode.SHOWN,
        ).ok
        for store in schedule.METRICS_BACKUPS
    ]
    return _verdict(all(out_of_init))


def _history(ctx: Context) -> Verdict:
    kept = baseline(ctx, readings.METRICS)
    now = _metrics(ctx)
    ctx.note(f'baseline  {readings.METRICS.shown(kept)}')
    ctx.note(f'now       {readings.METRICS.shown(now)}')
    return _verdict(readings.history_back(kept, now))


def _import_peaks(ctx: Context) -> Verdict:
    _ = ctx.answer('Stop the peak recorder with Ctrl-C, then press Enter')
    if ctx.run(['perf', 'import', str(PEAKS)], mode=Mode.SHOWN).ok:
        return PASS
    ctx.note(
        'the import refuses while vmagent holds what it queued; `drill rebuild` runs it again once that has drained'
    )
    return FAIL


# --------------------------------------------------------------------------
# The pass readings.
# --------------------------------------------------------------------------


def _physical_unchanged(ctx: Context) -> Verdict:
    clean = _preview(ctx, PHYSICAL)
    return _verdict(_compare(ctx, readings.OUTSIDE, _outside(ctx)) and clean)


def _dns_unchanged(ctx: Context) -> Verdict:
    clean = _preview(ctx, DNS)
    held = _compare(ctx, readings.OUTPUT_READINGS, _outputs(ctx))
    anchored = _compare(ctx, readings.ANCHOR_READINGS, _anchors(ctx))
    return _verdict(clean and held and anchored)


def _scoped(ctx: Context) -> Verdict:
    checks = cast('list[list[str]]', ctx.record.fact('scope checks') or [])
    ctx.note(f'{len(checks)} plan(s) read, {sum(1 for each in checks if each)} stepping outside the scope')
    return _verdict(bool(checks) and not any(checks))


def _enabled(ctx: Context) -> Verdict:
    listed = ctx.gh('workflow', 'list', '--repo', chain.REPOSITORY, '--all', '--json', 'path,state')
    if not isinstance(parsed := _json(ctx, listed, "the repository's workflows"), list):
        return NO_READING
    rows = [cast('dict[str, Any]', row) for row in cast('list[object]', parsed) if isinstance(row, dict)]
    states = {str(row.get('path')).rsplit('/', 1)[-1]: str(row.get('state')) for row in rows}
    ctx.note(f'{chain.WORKFLOW}: {states.get(chain.WORKFLOW)}')
    return _verdict(states.get(chain.WORKFLOW) == 'active')


def _pressure_through(ctx: Context) -> Verdict:
    """No node reports `DiskPressure` at the end of the run, as at the gate."""
    if (nodes := _items(ctx, ctx.kubectl('get', 'nodes', '-o', 'json'), 'the nodes')) is None:
        return NO_READING
    return _verdict(_no_disk_pressure(ctx, nodes))


def summary(ctx: Context) -> str:
    """The table of steps, verdicts and times that goes on the issue recording the run."""
    rows = ['| Step | Verdict | Started | Ended |', '| --- | --- | --- | --- |']
    for step in SEQUENCE:
        started, ended = ctx.record.times(step.key)
        rows.append(
            f'| {step.key}: {step.title} | {ctx.record.verdict(step.key) or "-"} | {started or "-"} | {ended or "-"} |'
        )
    for name in ('dispatched', 'applied'):
        if (url := record.as_mapping(ctx.record.fact(name)).get('url')) is not None:
            rows.append(f'\nThe {name} run: {url}')
    return '\n'.join(rows)


def _close(ctx: Context) -> Verdict:
    ctx.note(summary(ctx))
    ctx.note(
        "Still owed, and read after the record is gone: the history's backup job succeeding on its next run "
        '(operations.md §6.2), with pass condition 5; and no node reporting DiskPressure before then, with pass '
        'condition 3, which the drill read at the gate and at the end.'
    )
    got = ctx.answer(
        'Post the summary and the transcripts on the issue, then type `delete` to delete the run directory:'
    )
    if got != 'delete':
        return NO_READING
    ctx.record.closed = True
    shutil.rmtree(ctx.checkout / record.DIRECTORY)
    ctx.say(f'deleted {record.DIRECTORY}')
    return PASS


#: Where the run after an actual loss is designed, which step 0 cannot take.
LOSS_MODE = 'Aetf/kluster-ops#903'

#: The step whose sitting records each command it finished, and which a
#: dispatch starts over.
STEP_9 = '9'

#: The key the pass readings are recorded under, after the steps.
PASS_READINGS_KEY = 'pass'

STEPS: tuple[Step, ...] = (
    Step(
        '0',
        'the baseline, and the go',
        (
            Part('freeze', _freeze),
            Part('converged', _converged),
            Part('baselines', _take_baselines),
            Part('backup', _backup),
            Part('gateway-health', gateway_health),
            Part('capacity', _capacity),
            Part('go', _go),
        ),
        on_stop=_still_frozen,
    ),
    Step('1', 'the day-1 chain leaves state', (Part('destroy', _day1),)),
    Step('2', "the worker's disk loses its protection", (Part('unprotect', _unprotect),)),
    Step(
        '3',
        'the loss',
        (
            Part('capacity', _fresh_capacity),
            Part('terminate', _terminate),
            Part('left', _left),
            Part('gateway-health', gateway_health),
        ),
    ),
    Step('4', 'the state records the loss', (Part('refresh', _refresh_loss), Part('recorded', _loss_recorded))),
    Step('5', 'the rebuild', (Part('rebuild', _rebuild), Part('ephemeral', _ephemeral))),
    Step(
        '6',
        '`physical` converges',
        (
            Part('recorder', _restart_recorder),
            Part('converged', _physical_converges),
            Part('unchanged', _nothing_else_moved),
        ),
    ),
    Step(
        '7',
        'the readings that need no network plugin',
        (
            Part('clients', _clients),
            Part('versions', _versions),
            Part('link-local', _link),
            Part('etcd', _etcd),
            Part('fronts', _fronts),
            Part('ids', _volume_ids),
            Part('u-storage', _local_storage),
            Part('node label', _node_label),
        ),
    ),
    Step(
        '8',
        '`k8s-base` and `apps` from empty, by a dispatched chain',
        (Part('refresh', _refresh_cluster_stacks), Part('dispatch', _dispatch)),
    ),
    Step(
        '9',
        'the new controller answers, and one pull request seals',
        (
            Part('dispatched', _dispatched),
            Part('controller', _controller),
            Part('certificate', _certificate),
            Part('carry', _carry),
            Part('sitting', _sit),
            Part('copies', _copies),
            Part('merge', _merge),
            Part('chains', _chains),
            Part('gate', _gate),
            Part('stores', _stores),
            Part('history', _history),
            Part('peaks', _import_peaks),
        ),
    ),
)

PASS_READINGS = Step(
    PASS_READINGS_KEY,
    'the pass readings',
    (
        Part('gateway-health', gateway_health),
        Part('physical', _physical_unchanged),
        Part('dns', _dns_unchanged),
        Part('scope', _scoped),
        Part('disk pressure', _pressure_through),
        Part('deploy.yml', _enabled),
        Part('close', _close),
    ),
)

#: Every step in the order a run takes them.
SEQUENCE: tuple[Step, ...] = (*STEPS, PASS_READINGS)
