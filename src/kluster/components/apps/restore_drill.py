"""A database's restore drill: recover a throwaway cluster from the database's archive, check it, and remove it.

Run as the drill CronJob's one container by the drill image's own `python3`,
with its standard library alone, so it imports nothing of this repository:
`components.apps.database` mounts this file's text and the throwaway
cluster's manifest from the drill's ConfigMap, and everything else it reads
is the pod's environment.

A run removes what an earlier run left, applies the throwaway cluster, waits
for it to be ready, and checks it through `psql` in its primary: that the
database answers at all, then that it has left recovery, that the
application's table holds rows and that its extensions are there. Each check
that fails writes a `FAIL:` line of its own to stderr, naming what it could
not read or what it found, and the run exits 1; a run that passes ends in a
`passed:` line and exits 0. **The throwaway cluster is removed whatever
happened**, a TERM included: the kubelet sends one at the Job's deadline, and
the handler turns it into the exception that unwinds to the removal, which a
process that is PID 1 gets only from a handler it installs. The removal is a
foreground cascade, so the cluster's claims go before it does, removed by the
garbage collector as the operator's dependents; a removal that fails fails
the run too, since what it leaves holds a second copy of the database's
requests on its pool.
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys
from types import FrameType

#: The bound on each `kubectl` call but the wait for the cluster, past which
#: it is reported as not finished: every one of them asks the API once.
CALL_TIMEOUT = 300
#: What the run waits beyond the wait's own bound before it stops `kubectl`.
WAIT_MARGIN = 60


class Stopped(Exception):
    """The pod was asked to stop, at its deadline or by its deletion."""


def _stop(signum: int, _frame: FrameType | None) -> None:
    raise Stopped(signal.Signals(signum).name)


def say(message: str) -> None:
    print(message, flush=True)


def fail(message: str) -> None:
    print(f'FAIL: {message}', file=sys.stderr, flush=True)


def kubectl(*args: str, timeout: float = CALL_TIMEOUT) -> subprocess.CompletedProcess[str]:
    """One `kubectl` call, its output captured; one that does not finish within `timeout` is a failed call."""
    try:
        return subprocess.run(['kubectl', *args], capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(['kubectl', *args], 124, '', f'did not finish within {timeout}s')


def remove(namespace: str, drill: str) -> bool:
    """Remove the throwaway cluster and, before it, its claims; whether it is gone."""
    removed = kubectl(
        'delete', 'cluster', drill, '-n', namespace, '--ignore-not-found', '--cascade=foreground', '--wait=true'
    )
    if removed.returncode != 0:
        fail(f'{drill} could not be removed: {removed.stderr.strip()}')
        return False
    return True


class Recovered:
    """The throwaway cluster's primary, which every check queries through `psql`."""

    def __init__(self, namespace: str, pod: str, database: str) -> None:
        self.namespace = namespace
        self.pod = pod
        self.database = database

    def query(self, sql: str) -> tuple[str | None, str]:
        """What `sql` returns, unaligned and without headers, or `None` and why it could not be read."""
        done = kubectl(
            'exec',
            '-n',
            self.namespace,
            self.pod,
            '-c',
            'postgres',
            '--',
            'psql',
            '-U',
            'postgres',
            '-d',
            self.database,
            '-tAc',
            sql,
        )
        if done.returncode != 0:
            return None, done.stderr.strip()
        return done.stdout.strip(), ''


def recover(namespace: str, drill: str, server: str, manifest: str, ready: int) -> str | None:
    """Apply the throwaway cluster and wait for its primary, which is returned; `None` where a step failed."""
    say(f'recovering {drill} from the newest backup of {server}, waiting up to {ready}s for it to be ready')
    applied = kubectl('apply', '-n', namespace, '-f', manifest)
    if applied.returncode != 0:
        fail(f'{drill} could not be applied: {applied.stderr.strip()}')
        return None
    waited = kubectl(
        'wait',
        '-n',
        namespace,
        '--for=condition=Ready',
        f'cluster/{drill}',
        f'--timeout={ready}s',
        timeout=ready + WAIT_MARGIN,
    )
    if waited.returncode != 0:
        fail(f'{drill} did not become ready: {waited.stderr.strip()}')
        status = kubectl('get', 'cluster', drill, '-n', namespace, '-o', 'yaml')
        print(status.stdout or status.stderr, file=sys.stderr, flush=True)
        return None
    pods = kubectl(
        'get', 'pods', '-n', namespace, '-l', f'cnpg.io/cluster={drill},cnpg.io/instanceRole=primary', '-o', 'name'
    )
    if pods.returncode != 0:
        fail(f'the pods of {drill} could not be listed: {pods.stderr.strip()}')
        return None
    primaries = pods.stdout.split()
    if not primaries:
        fail(f'{drill} has no primary')
        return None
    return primaries[0].removeprefix('pod/')


def check(recovered: Recovered, drill: str, table: str, extensions: list[str]) -> int | None:
    """Every check on the recovered database, each failure on a line of its own; the table's rows where all pass."""
    answer, why = recovered.query('select 1')
    if answer != '1':
        fail(f'{recovered.database} does not answer in {recovered.pod}: {why or answer}')
        return None
    passed = True
    state, why = recovered.query('select pg_is_in_recovery()')
    if state is None:
        fail(f'whether {drill} left recovery could not be read: {why}')
        passed = False
    elif state != 'f':
        fail(f'{drill} is still in recovery')
        passed = False
    counted, why = recovered.query(f'select count(*) from "{table}"')
    rows = int(counted) if counted is not None and counted.isdigit() else None
    if counted is None:
        fail(f'{table} could not be read in {recovered.database}: {why}')
        passed = False
    elif rows is None or rows == 0:
        fail(f'{table} holds no rows ({counted})')
        passed = False
    installed, why = recovered.query('select extname from pg_extension')
    if installed is None:
        fail(f'the extensions of {recovered.database} could not be read: {why}')
        passed = False
    else:
        for extension in extensions:
            if extension not in installed.split():
                fail(f'the extension {extension} is missing')
                passed = False
    return rows if passed else None


def main() -> int:
    env = os.environ
    namespace, drill, server, database, table = (
        env[key] for key in ('NAMESPACE', 'DRILL', 'SERVER', 'DATABASE', 'TABLE')
    )
    extensions = env['EXTENSIONS'].split()
    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    rows: int | None = None
    try:
        say(f'removing what an earlier run left of {drill}')
        if remove(namespace, drill):
            pod = recover(namespace, drill, server, env['MANIFEST'], int(env['READY_TIMEOUT']))
            if pod is not None:
                say(f'checking {database} in {pod}')
                rows = check(Recovered(namespace, pod, database), drill, table, extensions)
    except Stopped as stopped:
        fail(f'the run was stopped by {stopped} before the drill finished')
        rows = None
    finally:
        # The removal runs to its end: a second TERM would cut it short and
        # leave the cluster up, and the kubelet's kill follows the grace
        # period whatever is done here.
        _ = signal.signal(signal.SIGTERM, signal.SIG_IGN)
        say(f'removing {drill} and its claims')
        removed = remove(namespace, drill)
    if rows is None or not removed:
        return 1
    say(f'passed: {database} recovered, {table} holds {rows} rows')
    return 0


if __name__ == '__main__':
    sys.exit(main())
