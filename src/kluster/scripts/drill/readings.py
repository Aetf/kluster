"""The rebuild's baselines, and the readings compared with them (operations.md §3.5).

**A reading that is not whole is not a reading**, and it is a value of its
own, `NoReading`, rather than an empty one: a read that failed, an export with
no protected resource in it, a query `doggo` got no answer to, a `metrics`
with no `rows`. Every comparison here answers no for it, on either side, so a
baseline taken from a failed read never agrees with a later read that failed
the same way, and step 0 refuses the go while any baseline holds one
(`Kind.unwhole`).

Each baseline is one `Kind`: how a reading of it is kept in `run.json`, shown
where it is taken, and judged whole. The readings are parsed here from what
the commands printed, and nothing here starts a command.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, cast

from kluster import conventions
from kluster.scripts.drill import scope
from kluster.scripts.drill.tools import Completed


@dataclass(frozen=True)
class NoReading:
    """A read that is no evidence either way, and why."""

    why: str


type Reading[T] = T | NoReading


def failed(done: Completed, what: str) -> NoReading:
    return NoReading(f'{what} failed: {done.said}')


def same[T](baseline: Reading[T], reading: Reading[T]) -> bool:
    """Whether a reading is its baseline: both whole, and equal."""
    return not isinstance(baseline, NoReading) and not isinstance(reading, NoReading) and baseline == reading


def json_stream(text: str) -> list[object]:
    """The JSON documents `text` holds one after another, as `talosctl -o json` prints a resource each."""
    decoder = json.JSONDecoder()
    found: list[object] = []
    at = 0
    while (at := _skip_space(text, at)) < len(text):
        document, at = decoder.raw_decode(text, at)
        found.append(cast('object', document))
    return found


def specification(document: object) -> dict[str, Any]:
    """The `spec` of one resource `talosctl -o json` printed, empty where it has none."""
    if not isinstance(document, dict):
        return {}
    spec = cast('dict[str, Any]', document).get('spec')
    return cast('dict[str, Any]', spec) if isinstance(spec, dict) else {}


def _skip_space(text: str, at: int) -> int:
    while at < len(text) and text[at].isspace():
        at += 1
    return at


# --------------------------------------------------------------------------
# `physical`'s state: what is protected, and a digest of what lies outside the scope.
# --------------------------------------------------------------------------


NOT_AN_EXPORT = NoReading('what the export printed is not an export')


def protected(export: Completed) -> Reading[frozenset[str]]:
    """Every protected resource of a `pulumi stack export`: never none for the stack as it stands."""
    if not export.ok:
        return failed(export, 'the export of physical')
    if (listed := scope.resources(export.out)) is None:
        return NOT_AN_EXPORT
    urns = frozenset(str(resource.get('urn')) for resource in listed if resource.get('protect'))
    return urns or NoReading('the export lists no protected resource')


#: The provider every dynamic resource of a stack is registered against. Its
#: state records the stack's whole configuration, which any `physical up`
#: rewrites to its commit's, so it is left out of `outside`.
DYNAMIC_PROVIDER_SUFFIX = '::pulumi:providers:pulumi-python::default'


def outside(export: Completed, within: re.Pattern[str]) -> Reading[str]:
    """A digest of every resource of a `--show-secrets` export the rebuild does not change.

    Each entry's URN, inputs, outputs and protection, sorted by URN, digested
    so that only the digest leaves the export. A failed export, or one with
    nothing outside the scope, digests nothing.
    """
    if not export.ok:
        return failed(export, 'the export of physical with its secrets')
    if (listed := scope.resources(export.out)) is None:
        return NOT_AN_EXPORT
    entries = sorted(
        (
            {key: resource.get(key) for key in ('urn', 'inputs', 'outputs', 'protect')}
            for resource in listed
            if not within.search(str(resource.get('urn')))
            and not str(resource.get('urn')).endswith(DYNAMIC_PROVIDER_SUFFIX)
        ),
        key=lambda entry: str(entry['urn']),
    )
    if not entries:
        return NoReading('the export holds nothing outside the scope')
    return hashlib.sha256(json.dumps(entries, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


# --------------------------------------------------------------------------
# The anchors, as a resolver outside the estate answers them.
# --------------------------------------------------------------------------

#: The resolver the anchors are read through: one outside every network
#: this estate runs.
RESOLVER = '@1.1.1.1'

#: The names a consumer outside the cluster dials, and the record types read
#: for each.
ANCHORS = tuple(
    (f'{name}.{conventions.ZONE_PRIMARY}', kind)
    for name in (conventions.ANCHOR_CLUSTER, conventions.ANCHOR_VIP1)
    for kind in ('A', 'AAAA')
)

#: The anchors that hold no record of the type: the dedicated VIP is IPv4 only.
EMPTY_ANCHORS = frozenset({(f'{conventions.ANCHOR_VIP1}.{conventions.ZONE_PRIMARY}', 'AAAA')})


@dataclass(frozen=True)
class Anchor:
    """One name and record type, and what it resolved to: addresses, none, or no answer at all."""

    name: str
    kind: str
    addresses: tuple[str, ...] | None


def anchor(name: str, kind: str, done: Completed) -> Anchor:
    """`doggo --short`'s answer: the addresses it printed, sorted, or None where it got no answer."""
    if not done.ok:
        return Anchor(name, kind, None)
    return Anchor(name, kind, tuple(sorted(line.strip() for line in done.out.splitlines() if line.strip())))


# --------------------------------------------------------------------------
# A node volume's partition, as Talos discovers it.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Partition:
    name: str
    #: The filesystem's UUID. None where discovery has not probed the
    #: partition since Talos formatted it, as on the boot that provisioned it.
    uuid: str | None
    #: The partition's GPT GUID.
    partition_uuid: str | None


@dataclass(frozen=True)
class Ids:
    """Every discovered partition labelled `u-<volume>` on its node."""

    partitions: tuple[Partition, ...]

    @property
    def found(self) -> int:
        return len(self.partitions)


def _present(value: object) -> str | None:
    return None if value in (None, '', 'null') else str(value)


def ids(volume: str, done: Completed) -> Reading[Ids]:
    """`talosctl get discoveredvolumes -o json`, narrowed to `volume`'s partition; a failed read counts nothing."""
    if not done.ok:
        return failed(done, 'the read of the discovered volumes')
    label = f'u-{volume}'
    try:
        documents = json_stream(done.out)
    except ValueError:
        return NoReading('what the read of the discovered volumes printed is not JSON')
    specs = [specification(document) for document in documents]
    return Ids(
        tuple(
            Partition(str(spec.get('name')), _present(spec.get('uuid')), _present(spec.get('partition_uuid')))
            for spec in specs
            if spec.get('partition_label') == label
        )
    )


def ids_pass(baseline: Reading[Ids], rebuilt: Reading[Ids]) -> bool:
    """operations.md §3.5 step 7's rule for a node volume's partition, before and after the rebuild.

    Both find one partition with the same GPT GUID, and the rebuilt node reads
    a filesystem UUID, the baseline's wherever the baseline holds one. A
    baseline with none was read on the boot that provisioned the volume.
    """
    if isinstance(baseline, NoReading) or isinstance(rebuilt, NoReading):
        return False
    if baseline.found != 1 or rebuilt.found != 1:
        return False
    (before,), (after,) = baseline.partitions, rebuilt.partitions
    if before.partition_uuid is None or after.partition_uuid != before.partition_uuid:
        return False
    if after.uuid is None:
        return False
    return before.uuid is None or after.uuid == before.uuid


# --------------------------------------------------------------------------
# The sealing certificate, and the metrics history.
# --------------------------------------------------------------------------

CERTIFICATE_HEAD = '-----BEGIN CERTIFICATE-----'


def certificates(pem: str) -> int:
    return pem.count(CERTIFICATE_HEAD)


def sealing(done: Completed) -> Reading[str]:
    """`kubeseal --fetch-cert`'s certificate, kept as it printed it."""
    if not done.ok:
        return failed(done, 'the fetch of the sealing certificate')
    return done.out if certificates(done.out) else NoReading('the fetch printed no certificate')


@dataclass(frozen=True)
class Metrics:
    """The history store: the rows it holds, and the time of its earliest sample."""

    rows: int
    earliest: str


#: The series whose sum is the rows the history store holds: every line that
#: starts so, its second field summed, the rule its backup job counts by
#: (`components/monitoring/shell/backup-check.sh`).
ROWS_PREFIX = 'vm_rows{type="storage/'


#: The query for the earliest sample: of `node_load1`, which the history holds
#: from its first day (operations.md §3.5).
EARLIEST_QUERY = '/api/v1/query?query=min(tfirst_over_time(node_load1%5B100y%5D))'


def metrics(exposition: Completed, earliest: Completed) -> Reading[Metrics]:
    """The rows, counted only over a read that succeeded, and the earliest sample, where the query found one."""
    if not exposition.ok:
        return failed(exposition, "the read of the history store's metrics")
    if not earliest.ok:
        return failed(earliest, "the query of the history's earliest sample")
    try:
        rows = sum(float(line.split()[1]) for line in exposition.out.splitlines() if line.startswith(ROWS_PREFIX))
        answer = cast('dict[str, Any]', json.loads(earliest.out))
        result = cast('list[dict[str, Any]]', cast('dict[str, Any]', answer.get('data') or {}).get('result') or [])
        sample = str(cast('list[object]', result[0]['value'])[1]) if result else None
    except (ValueError, IndexError, KeyError, TypeError, AttributeError):
        return NoReading("what the history store answered is not an exposition and a query's answer")
    if sample is None:
        return NoReading('the query of the earliest sample found none')
    return Metrics(rows=round(rows), earliest=sample)


def history_back(baseline: Reading[Metrics], reading: Reading[Metrics]) -> bool:
    """Whether the history came back: its baseline's earliest sample, and no fewer rows."""
    if isinstance(baseline, NoReading) or isinstance(reading, NoReading):
        return False
    return reading.earliest == baseline.earliest and reading.rows >= baseline.rows


#: The outputs of `physical` a consumer outside the cluster holds: the
#: balancer's two addresses, the reserved VIP and the worker's GUA.
OUTPUTS = (
    conventions.PHYSICAL_OUTPUTS.cluster_endpoint,
    conventions.PHYSICAL_OUTPUTS.cluster_endpoint_v6,
    conventions.PHYSICAL_OUTPUTS.vip1,
    conventions.PHYSICAL_OUTPUTS.worker_gua,
)


def outputs(read: Mapping[str, Completed]) -> Reading[dict[str, str]]:
    """Each of `OUTPUTS` as `physical stack output` printed it; no reading where one failed or holds nothing."""
    values: dict[str, str] = {}
    for name in OUTPUTS:
        done = read[name]
        if not done.ok:
            return failed(done, f'the read of the {name} output')
        if not done.out.strip():
            return NoReading(f'the {name} output holds nothing')
        values[name] = done.out.strip()
    return values


# --------------------------------------------------------------------------
# The baselines, as `run.json` keeps them.
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Kind[T]:
    """One baseline: how its reading is kept, shown, and judged whole."""

    name: str
    dump: Callable[[T], object]
    load: Callable[[Any], T]
    show: Callable[[T], str]
    #: Why a whole read of it is still not a baseline, or None.
    unwhole: Callable[[T], str | None] = lambda _: None

    def keep(self, reading: Reading[T]) -> object:
        if isinstance(reading, NoReading):
            return {'no reading': reading.why}
        return {'reading': self.dump(reading)}

    def restore(self, kept: object) -> Reading[T]:
        if not isinstance(kept, dict):
            return NoReading('not taken')
        entry = cast('dict[str, Any]', kept)
        if 'reading' in entry:
            return self.load(entry['reading'])
        return NoReading(str(entry.get('no reading', 'not taken')))

    def shown(self, reading: Reading[T]) -> str:
        if isinstance(reading, NoReading):
            return f'{self.name}: no reading ({reading.why})'
        return f'{self.name}: {self.show(reading)}'

    def why_not_whole(self, reading: Reading[T]) -> str | None:
        """Why `reading` is no baseline, or None where it is one."""
        return reading.why if isinstance(reading, NoReading) else self.unwhole(reading)


def _anchors_unwhole(anchors: tuple[Anchor, ...]) -> str | None:
    unanswered = [f'{each.name} {each.kind}' for each in anchors if each.addresses is None]
    if unanswered:
        return f'no answer for {", ".join(unanswered)}'
    empty = [
        f'{each.name} {each.kind}'
        for each in anchors
        if not each.addresses and (each.name, each.kind) not in EMPTY_ANCHORS
    ]
    return f'no address for {", ".join(empty)}' if empty else None


def _ids_unwhole(found: Ids) -> str | None:
    if found.found != 1:
        return f'{found.found} partitions found, not one'
    return None if found.partitions[0].partition_uuid else 'the partition reads no partition UUID'


def _show_ids(found: Ids) -> str:
    return '; '.join(
        [f'{found.found} found']
        + [
            f'{each.name} uuid={each.uuid or "null"} partition_uuid={each.partition_uuid or "null"}'
            for each in found.partitions
        ]
    )


def _load_anchors(kept: Any) -> tuple[Anchor, ...]:
    return tuple(
        Anchor(str(name), str(kind), None if addresses is None else tuple(cast('list[str]', addresses)))
        for name, kind, addresses in cast('list[list[Any]]', kept)
    )


def _load_ids(kept: Any) -> Ids:
    return Ids(tuple(Partition(*cast('list[Any]', each)) for each in cast('list[Any]', kept)))


PROTECTED = Kind[frozenset[str]](
    'protected',
    dump=sorted,
    load=lambda kept: frozenset(cast('list[str]', kept)),
    show=lambda urns: f'{len(urns)} resources',
)
OUTSIDE = Kind[str]('outside', dump=str, load=str, show=str)
ANCHOR_READINGS = Kind[tuple[Anchor, ...]](
    'anchors',
    dump=lambda anchors: [
        [each.name, each.kind, None if each.addresses is None else list(each.addresses)] for each in anchors
    ],
    load=_load_anchors,
    show=lambda anchors: '; '.join(
        f'{each.name} {each.kind} {" ".join(each.addresses) if each.addresses is not None else "no reading"}'
        for each in anchors
    ),
    unwhole=_anchors_unwhole,
)
SEALING = Kind[str](
    'sealing',
    dump=str,
    load=str,
    show=lambda pem: f'{certificates(pem)} certificate(s)',
    unwhole=lambda pem: None if certificates(pem) == 1 else f'{certificates(pem)} certificates, not one',
)
METRICS = Kind[Metrics](
    'metrics',
    dump=lambda read: {'rows': read.rows, 'earliest': read.earliest},
    load=lambda kept: Metrics(rows=int(kept['rows']), earliest=str(kept['earliest'])),
    show=lambda read: f'rows {read.rows}; earliest {read.earliest}',
)
OUTPUT_READINGS = Kind[dict[str, str]](
    'outputs',
    dump=dict,
    load=lambda kept: {str(name): str(value) for name, value in cast('dict[str, Any]', kept).items()},
    show=lambda values: '; '.join(f'{name} {value}' for name, value in values.items()),
)


def ids_kind(volume: str) -> Kind[Ids]:
    """The baseline of one node volume's partition, of each row of `conventions.NODE_VOLUMES`."""
    return Kind[Ids](
        f'ids {volume}',
        dump=lambda found: [[each.name, each.uuid, each.partition_uuid] for each in found.partitions],
        load=_load_ids,
        show=_show_ids,
        unwhole=_ids_unwhole,
    )


def volumes() -> Sequence[str]:
    return tuple(conventions.NODE_VOLUMES)
