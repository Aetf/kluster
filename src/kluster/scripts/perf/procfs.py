"""What a control plane's `/proc` files say, as the node exporter's series of the same facts.

The recorder reads a node's kernel counters through Talos rather than
through the node exporter, which is not running in the minutes it records,
and writes each under the name and in the unit the node exporter gives it, so
a reading over the node exporter's series reads a recorded period the same
way. Each parser here is one collector of the node exporter's, for the
families the history store keeps (`components.monitoring.stores.HISTORY_CLASS`)
and nothing else: the recorder writes into that store directly, past the
relabeling that keeps the class for vmagent.

The units are the kernel's, converted as the node exporter converts them:
a pressure total is microseconds, a disk's sectors are 512 bytes whatever
the device's own sector size, a disk's times are milliseconds, and a
`/proc/meminfo` figure is kibibytes.
"""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass

__all__ = ('FILES', 'PRESSURE', 'Sample', 'diskstats', 'loadavg', 'meminfo', 'pressure')


@dataclass(frozen=True)
class Sample:
    """One value of one series: its name, its own labels, and the value."""

    name: str
    labels: tuple[tuple[str, str], ...]
    value: float


#: Each pressure resource, and the name the node exporter gives its `some` and
#: its `full` totals (`collector/pressure_linux.go`): the share of time some
#: task waited, and the share in which every task stalled.
PRESSURE: Mapping[str, tuple[str, str]] = {
    'cpu': ('node_pressure_cpu_waiting_seconds_total', 'node_pressure_cpu_stalled_seconds_total'),
    'io': ('node_pressure_io_waiting_seconds_total', 'node_pressure_io_stalled_seconds_total'),
    'memory': ('node_pressure_memory_waiting_seconds_total', 'node_pressure_memory_stalled_seconds_total'),
}

_PSI_TOTAL = re.compile(r'^(some|full) .*\btotal=(\d+)\b')
_MICROSECONDS = 1e-6


def pressure(resource: str, text: str) -> list[Sample]:
    """`/proc/pressure/<resource>`: each line's `total`, in seconds.

    A kernel that writes no `full` line for the CPU writes no stalled series,
    as the node exporter does.
    """
    waiting, stalled = PRESSURE[resource]
    names = {'some': waiting, 'full': stalled}
    found: list[Sample] = []
    for line in text.splitlines():
        matched = _PSI_TOTAL.match(line)
        if matched is not None:
            found.append(Sample(names[matched[1]], (), int(matched[2]) * _MICROSECONDS))
    return found


#: The devices the node exporter leaves out by default
#: (`--collector.diskstats.device-exclude`): partitions, and RAM, loop and
#: floppy devices, so a disk is counted once, by itself.
DEVICE_EXCLUDE = re.compile(r'^(z?ram|loop|fd|(h|s|v|xv)d[a-z]|nvme\d+n\d+p)\d+$')

#: A sector, as `/proc/diskstats` counts them: 512 bytes on every device.
_SECTOR = 512
_MILLISECONDS = 1e-3

#: The first eleven statistics of a `/proc/diskstats` line, after the major and
#: minor numbers and the device's name (the kernel's
#: `Documentation/admin-guide/iostats.rst`), each as the node exporter names
#: it and the factor it scales the field by.
_DISK_FIELDS: tuple[tuple[str, float], ...] = (
    ('node_disk_reads_completed_total', 1),
    ('node_disk_reads_merged_total', 1),
    ('node_disk_read_bytes_total', _SECTOR),
    ('node_disk_read_time_seconds_total', _MILLISECONDS),
    ('node_disk_writes_completed_total', 1),
    ('node_disk_writes_merged_total', 1),
    ('node_disk_written_bytes_total', _SECTOR),
    ('node_disk_write_time_seconds_total', _MILLISECONDS),
    ('node_disk_io_now', 1),
    ('node_disk_io_time_seconds_total', _MILLISECONDS),
    ('node_disk_io_time_weighted_seconds_total', _MILLISECONDS),
)


def diskstats(text: str) -> list[Sample]:
    """`/proc/diskstats`: each disk's first eleven statistics, labeled `device`."""
    found: list[Sample] = []
    for line in text.splitlines():
        fields = line.split()
        if len(fields) < 3 + len(_DISK_FIELDS) or DEVICE_EXCLUDE.match(fields[2]):
            continue
        device = (('device', fields[2]),)
        found.extend(
            Sample(name, device, int(raw) * factor)
            for (name, factor), raw in zip(_DISK_FIELDS, fields[3:], strict=False)
        )
    return found


def loadavg(text: str) -> list[Sample]:
    """`/proc/loadavg`: the three load averages."""
    one, five, fifteen = text.split()[:3]
    return [
        Sample('node_load1', (), float(one)),
        Sample('node_load5', (), float(five)),
        Sample('node_load15', (), float(fifteen)),
    ]


#: The `/proc/meminfo` fields the history keeps.
MEMORY_FIELDS = ('MemAvailable', 'MemTotal', 'Dirty', 'Writeback')
_KIBIBYTE = 1024


def meminfo(text: str) -> list[Sample]:
    """`/proc/meminfo`: the fields of `MEMORY_FIELDS`, in bytes, as `node_memory_<field>_bytes`."""
    found: list[Sample] = []
    for line in text.splitlines():
        field, _, rest = line.partition(':')
        if field in MEMORY_FIELDS:
            found.append(Sample(f'node_memory_{field}_bytes', (), int(rest.split()[0]) * _KIBIBYTE))
    return found


def _pressure(resource: str) -> Callable[[str], list[Sample]]:
    return lambda text: pressure(resource, text)


#: Every file the recorder reads through Talos, and how it is read.
FILES: Mapping[str, Callable[[str], list[Sample]]] = {
    **{f'/proc/pressure/{resource}': _pressure(resource) for resource in PRESSURE},
    '/proc/diskstats': diskstats,
    '/proc/loadavg': loadavg,
    '/proc/meminfo': meminfo,
}
