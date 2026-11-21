"""`perf`: the command line.

    perf record <file>
    perf import <file>

Run from the root of the checkout that holds `.credentials/`, under `mise x`,
which puts the pinned `talosctl`, `kubectl` and `pulumi` on `PATH`.
`--kubeconfig` and `--talosconfig` default to where the operator keeps them
(`cluster.KUBECONFIG`, `cluster.TALOSCONFIG`).
"""

from __future__ import annotations

import argparse
import logging
from pathlib import Path

from kluster.lib import pulumi_cli, stack_addresses
from kluster.lib.monitoring.readings import READINGS
from kluster.scripts import manual_run
from kluster.scripts.credentials import pulumi_config
from kluster.scripts.perf import cluster, exposition, history, record

log = logging.getLogger(__name__)

#: The exit of a run that did what it was asked.
DONE = 0
#: The exit of a run refused before it recorded or wrote anything.
REFUSED = 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='perf',
        description=(
            "Record the control planes' peaks the cluster cannot see of itself, and import the recording into the "
            'metrics history (Aetf/kluster-ops#790 §4.5).'
        ),
        epilog=(
            'When: around every run that creates nodes or applies k8s-base from empty -- a bring-up, a control '
            "plane's or the worker's replacement, a whole-cluster rebuild -- from the moment physical holds the "
            "control planes' addresses; the import once "
            'the history store runs and vmagent has sent it what it queued. docs/operations.md §8.5 is the procedure.'
        ),
    )
    _ = parser.add_argument(
        '--kubeconfig', type=Path, default=cluster.KUBECONFIG, help=f'default: {cluster.KUBECONFIG}'
    )
    commands = parser.add_subparsers(dest='command', required=True)
    recording = commands.add_parser(
        'record',
        help='sample every control plane until stopped (Ctrl-C), appending to the file',
        description=(
            f"Every {record.INTERVAL.total_seconds():g} s, read each control plane's pressure, disk statistics, load "
            "and memory through Talos and etcd's metrics through the API server, and append them to the file, until "
            "SIGINT or SIGTERM, once the reads under way have answered. The control planes are `physical`'s "
            '`node_private_ips`, read when it starts; a node whose hostname is not its name has its files left out.'
        ),
    )
    _ = recording.add_argument('file', type=Path, help='the recording, appended to')
    _ = recording.add_argument(
        '--talosconfig', type=Path, default=cluster.TALOSCONFIG, help=f'default: {cluster.TALOSCONFIG}'
    )
    importing = commands.add_parser(
        'import',
        help='write a recording into the history store, where the store holds nothing near it',
        description=(
            'Write the recording into the history store through the API server, leaving out every line inside a '
            "stretch of the cluster's own scrape of the same node and job, and every line an earlier import wrote, "
            "each stretch of the recording between two of the cluster's a series of its own. Refused while vmagent "
            'holds data queued for the stores.'
        ),
    )
    _ = importing.add_argument('file', type=Path, help='a recording `perf record` wrote')
    return parser


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    args = build_parser().parse_args(argv)
    kubeconfig: Path = args.kubeconfig.expanduser()
    api = cluster.Kubectl(kubeconfig)
    if args.command == 'record':
        return _record(args.file, cluster.Talosctl(args.talosconfig.expanduser()), api)
    return _import(args.file, cluster.Kubectl(kubeconfig, timeout=IMPORT_TIMEOUT))


#: The bound of one import request, a `history.CHUNK` of lines through the
#: API server: a stop-loss.
IMPORT_TIMEOUT = 300.0


def _record(path: Path, talos: cluster.Talos, api: cluster.Api) -> int:
    log.info("reading the control planes from `physical`'s outputs")
    try:
        nodes = cluster.control_planes()
    except (
        pulumi_cli.PulumiRefused,
        pulumi_config.MachineRefused,
        manual_run.CheckRefused,
        stack_addresses.UnusableAddressOutput,
        ValueError,
    ) as exc:
        log.error('no control planes to record: %s', exc)
        return REFUSED
    recorder = record.Recorder(nodes=nodes, talos=talos, api=api, keep=exposition.etcd_families(READINGS))
    stop = record.stopping()
    log.info(
        'recording %s every %gs into %s; stop with Ctrl-C',
        ', '.join(node.name for node in nodes),
        record.INTERVAL.total_seconds(),
        path,
    )
    with record.appending(path) as out:
        recorded = record.record(recorder, out, wait=stop.wait)
    log.info(
        'recorded %d rounds into %s: %s',
        recorded.rounds,
        path,
        ', '.join(f'{node} {count} lines' for node, count in sorted(recorded.lines.items())) or 'no line',
    )
    return DONE


def _import(path: Path, api: cluster.Api) -> int:
    try:
        imported = history.import_file(path, api)
    except (exposition.NotRecorded, history.QueueNotDrained, cluster.Unanswered, OSError, ValueError) as exc:
        log.error('%s was not imported whole: %s', path, exc)
        return REFUSED
    for key in sorted(imported.written.keys() | imported.scraped.keys() | imported.imported.keys()):
        node, job = key
        log.info(
            '%s on %s: %d lines written, %d left out as the cluster scraped them, %d as an earlier import wrote them',
            job,
            node,
            imported.written.get(key, 0),
            imported.scraped.get(key, 0),
            imported.imported.get(key, 0),
        )
    return DONE
