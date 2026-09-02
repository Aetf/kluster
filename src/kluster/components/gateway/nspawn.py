"""The nspawn runtime: what turns a directory on the device into a running machine.

The framework the container services run on, in the same relation to them as a
cluster is to its workloads. It owns no service and knows no image: what it
owns is the shape of `machines/<name>/`, the two boot-chain scripts that
converge it, the watchdog that keeps a started machine attached to its bridge,
and the one command that puts a machine back on the root filesystem the last
push displaced.

**Everything one machine is lives in one directory**, so that a machine can be
inspected, moved or deleted whole:

```text
machines/<name>/
    rootfs/                 the tree systemd-nspawn boots, unpacked from the pin
    rootfs.digest           which published artifact that tree came from
    state/                  the writable state, bind-mounted into the container
    <name>.nspawn           the machine's settings, mirrored into /etc/systemd/nspawn
    stamp                   what the converger last acted on
    <other files>           whatever the machine mounts or is seeded from
```

Two scripts converge it, both delivered through the persistence layer because
both manipulate systemd's own configuration:

-   **`30-nspawn-units.sh`** mirrors each machine's settings file into
    `/etc/systemd/nspawn`, where `systemd-nspawn@.service` reads it. That
    directory is wholly this program's, unlike the unit store, so the mirror
    removes what has no source.
-   **`40-machines.sh`** links each machine's root filesystem where systemd
    looks for it, installs an initial state into a state directory that has
    never held one, and restarts a machine when something that defines it
    changed. It runs at boot with nothing else present, and again as the
    post-apply hook of every file a machine is made of, so the recovery path is
    the path every apply exercises.

**The machine set is unordered.** Which machine is actuated when is a push-time
constraint — the machine carrying the deployment's own session must go last —
and that belongs to the dependency graph the push has, not to a script that
runs at boot where no apply is in flight.

**`systemd-nspawn@.service` is the unit, not a unit per machine.** It is
systemd's own template, so what a machine says about itself is said in its
settings file rather than in a unit this program writes; `machinectl` and
`systemctl` see the machines the same way they see any other.

**A failed push rolls back and stays failed.** A machine's post-apply hook does
not stop at converging: it asks whether the machine reached active, and if it
did not it swaps the root filesystem back to the tree this push displaced and
exits non-zero. The device is left running what it was running, the resource
fails, and the next preview still has the work to do.
"""

from __future__ import annotations

import shlex
from collections.abc import Sequence
from dataclasses import dataclass
from typing import ClassVar, final

import pulumi

from kluster import conventions
from kluster.components.gateway import persistence
from kluster.components.gateway.persistence import DevicePersistence
from kluster.lib import templates
from kluster.providers.device_files.provider import SUPERSEDED_SUFFIX, DeviceFile, marker_path
from putils import Component

__all__ = (
    'LIVE_MACHINES_DIR',
    'LIVE_NSPAWN_DIR',
    'MACHINES',
    'MACHINES_SCRIPT',
    'MARKER_SUFFIX',
    'NSPAWN_SUFFIX',
    'NSPAWN_UNITS_SCRIPT',
    'REJECTED_SUFFIX',
    'ROLLBACK_PROGRAM',
    'ROOTFS',
    'SKELETON',
    'STAMP',
    'STATE',
    'UNIT_TEMPLATE',
    'WATCHDOG_UNIT',
    'WATCHDOG_WORKER',
    'Machine',
    'NspawnRuntime',
    'Placement',
    'converge',
    'machine_file',
    'machine_hook',
    'machine_path',
    'machine_unit',
    'machines_script',
    'nspawn_path',
    'nspawn_units_script',
    'rollback_program',
    'rootfs_path',
    'stamp_path',
    'state_path',
    'watchdog_unit',
    'watchdog_worker',
)

# ---------------------------------------------------------------------------
# Where the runtime lives on the device
# ---------------------------------------------------------------------------

#: The directory of the custom root this layer asks the mechanism for, and the
#: root every machine's directory sits under. Stated here rather than in
#: `conventions` because nothing outside the gateway derives a path from it:
#: the layout below is the runtime's own, the way the boot chain's is the
#: persistence layer's.
SKELETON = 'machines'
MACHINES = persistence.skeleton_path(SKELETON)

#: The two directories off `/data` that hold what a machine needs to run, and
#: which a firmware update therefore takes away: the settings
#: `systemd-nspawn@.service` reads, and the roots systemd resolves a machine
#: name through.
LIVE_NSPAWN_DIR = '/etc/systemd/nspawn'
LIVE_MACHINES_DIR = '/var/lib/machines'

#: The two scripts this layer is. The numbers are the boot order: the settings
#: have to be in place before a machine is started against them.
NSPAWN_UNITS_SCRIPT = '30-nspawn-units.sh'
MACHINES_SCRIPT = '40-machines.sh'

#: The watchdog, as the local rule shapes it: a long-running monitor that
#: configures nothing of systemd's, so it is a unit and an executable rather
#: than a script in the boot chain. What it does is re-attach a container's
#: `vb-*` interface to its bridge, which this firmware drops after a restart.
WATCHDOG_UNIT = 'nspawn-bridge-watchdog.service'
WATCHDOG_WORKER = 'nspawn-bridge-watchdog.sh'

#: The one command a rollback is, on the device: the manual door as well as the
#: one the health gate takes.
ROLLBACK_PROGRAM = 'machine-rollback'

#: systemd's own template unit, which is what runs a machine. A machine states
#: what it needs in its settings file, so there is no unit per machine to
#: write, install or retire.
UNIT_TEMPLATE = 'systemd-nspawn@'

#: The names inside `machines/<name>/` that belong to the runtime and to the
#: push. Everything else in there is the machine's own (`machine_file`).
ROOTFS = 'rootfs'
STATE = 'state'
STAMP = 'stamp'
NSPAWN_SUFFIX = '.nspawn'

#: What the push appends to a tree's path for the marker naming the pin that
#: tree came from. Taken from the provider that writes it rather than restated,
#: because a rollback takes the marker away with the swap and a suffix that
#: disagreed would take nothing away. The push's other suffix — the tree it
#: displaced — is the provider's constant, used as it stands.
MARKER_SUFFIX = marker_path('')

#: Where the tree being rolled *out of* waits while the swap happens. A swap
#: needs a third name, and this one exists only between two renames.
REJECTED_SUFFIX = '.kluster-rejected'


def machine_path(machine: str) -> str:
    """One machine's directory: everything that machine is, in one place."""
    return f'{MACHINES}/{machine}'


def rootfs_path(machine: str) -> str:
    """The tree the machine boots, which the push owns and replaces whole."""
    return f'{machine_path(machine)}/{ROOTFS}'


def state_path(machine: str) -> str:
    """The writable state, bind-mounted in so a new tree keeps the identity."""
    return f'{machine_path(machine)}/{STATE}'


def nspawn_path(machine: str) -> str:
    """The machine's settings, as `30-nspawn-units.sh` finds them."""
    return f'{machine_path(machine)}/{machine}{NSPAWN_SUFFIX}'


def stamp_path(machine: str) -> str:
    """What `40-machines.sh` last acted on, as a checksum over the stamped set."""
    return f'{machine_path(machine)}/{STAMP}'


def machine_unit(machine: str) -> str:
    """The unit that runs the machine: systemd's template, instanced by name."""
    return f'{UNIT_TEMPLATE}{machine}.service'


def machine_file(machine: str, name: str) -> str:
    """One file of the machine's own, beside the pieces the runtime keeps.

    A name the runtime or the push already uses is refused rather than
    delivered: the two would be one path, and whichever wrote last would decide
    whether the machine booted a configuration file or a root filesystem.
    """
    reserved = (STATE, STAMP, f'{machine}{NSPAWN_SUFFIX}')
    if name in reserved or name.startswith(ROOTFS):
        raise ValueError(
            f'{name!r} is a name the nspawn runtime keeps under {machine_path(machine)}: '
            f'{", ".join((*reserved, f"{ROOTFS}*"))} are the machine itself, not files it reads'
        )
    return f'{machine_path(machine)}/{name}'


# ---------------------------------------------------------------------------
# What runs after a machine's file lands
# ---------------------------------------------------------------------------


def converge() -> str:
    """Mirror the settings, then converge the machines — the boot chain's own path.

    Both scripts, in the order the boot chain runs them, because a machine
    started against settings that have not been mirrored yet is a machine on
    the wrong network.
    """
    return f'{persistence.on_boot_hook(NSPAWN_UNITS_SCRIPT)}; {persistence.on_boot_hook(MACHINES_SCRIPT)}'


def machine_hook(machine: str, path: str) -> str:
    """Converge, then hold this machine to having come up — or put it back.

    The health gate is what makes a broken push visible at the moment it
    breaks: converging is not evidence that the machine runs, so the hook asks
    systemd, and a machine that did not reach active is rolled back onto the
    tree this push displaced and the operation fails. Failing is half the
    point — the resource is not recorded as applied, so the next preview still
    has the work to do, and an artifact whose hook failed withdraws its digest
    marker as well.

    `path` is the file whose delivery just ran this hook, and the gate is
    conditional on it because the same command runs after the delete: a file
    this program has stopped declaring is gone, and a machine being retired is
    a machine that is *supposed* not to be active.
    """
    return (
        f'{converge()}; '
        f'if [ -e {shlex.quote(path)} ]; then '
        f'systemctl is-active --quiet {shlex.quote(machine_unit(machine))} || '
        f'{{ {shlex.quote(persistence.executable_path(ROLLBACK_PROGRAM))} {shlex.quote(machine)}; exit 1; }}; fi'
    )


# ---------------------------------------------------------------------------
# What the runtime converges
# ---------------------------------------------------------------------------


@final
@dataclass(frozen=True)
class Placement:
    """Where a machine's initial state is delivered from, and where it lands."""

    source: str
    destination: str


@final
@dataclass(frozen=True)
class Machine:
    """One machine as the runtime deals with it, whatever image it runs.

    The runtime knows a machine by its name, by the paths whose contents decide
    that it must be restarted, and by the file it is seeded with where it has
    one. What the machine *is* — its image, its addressing, what it mounts — is
    the workload's business and never reaches here.
    """

    name: str
    #: The paths the machine's content stamp covers. A change to any of them is
    #: what makes `40-machines.sh` restart it, and nothing else does.
    stamped: tuple[str, ...]
    initial_state: Placement | None


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


@final
@dataclass(frozen=True)
class _NspawnUnitsParams:
    """What `30-nspawn-units.sh.j2` reads: the two directories it mirrors between."""

    cluster: str
    machines_root: str
    live_nspawn_dir: str
    suffix: str


@final
@dataclass(frozen=True)
class _MachinesParams:
    """What `40-machines.sh.j2` reads, with the machines as an unordered set."""

    cluster: str
    machines_root: str
    live_machines_dir: str
    unit_template: str
    rootfs: str
    state: str
    stamp: str
    machines: tuple[Machine, ...]


@final
@dataclass(frozen=True)
class _RollbackParams:
    """What `machine-rollback.j2` reads: the layout, and the push's own suffixes."""

    cluster: str
    machines_root: str
    unit_template: str
    rootfs: str
    marker_suffix: str
    superseded_suffix: str
    rejected_suffix: str


def nspawn_units_script() -> str:
    """The boot-chain script that mirrors each machine's settings where systemd reads them.

    A true mirror, deletions included: the live directory holds nothing that is
    not this program's, so a machine retired here is retired on a recovery boot
    too rather than only on the push that retired it.
    """
    return templates.render(
        persistence.TEMPLATE_PACKAGE,
        f'templates/{NSPAWN_UNITS_SCRIPT}.j2',
        _NspawnUnitsParams(
            cluster=conventions.CLUSTER_NAME,
            machines_root=MACHINES,
            live_nspawn_dir=LIVE_NSPAWN_DIR,
            suffix=NSPAWN_SUFFIX,
        ),
    )


def machines_script(machines: Sequence[Machine]) -> str:
    """The boot-chain script that links, seeds, stamps and starts the machines.

    The set is sorted here, so the file the device holds is a function of which
    machines are declared rather than of the order a caller listed them in.
    That the order carries no meaning is the point: the one ordering constraint
    there is belongs to the push, which has a dependency graph to say it in.
    """
    return templates.render(
        persistence.TEMPLATE_PACKAGE,
        f'templates/{MACHINES_SCRIPT}.j2',
        _MachinesParams(
            cluster=conventions.CLUSTER_NAME,
            machines_root=MACHINES,
            live_machines_dir=LIVE_MACHINES_DIR,
            unit_template=UNIT_TEMPLATE,
            rootfs=ROOTFS,
            state=STATE,
            stamp=STAMP,
            machines=tuple(sorted(machines, key=lambda machine: machine.name)),
        ),
    )


def rollback_program() -> str:
    """The command that puts one machine back on the tree the last push displaced.

    It swaps rather than restores, so running it twice returns to where it
    started, and it takes the digest marker away with the swap: the tree is no
    longer the one the pin names, and a marker that still claimed it would make
    the next preview see nothing to do.
    """
    return templates.render(
        persistence.TEMPLATE_PACKAGE,
        f'templates/{ROLLBACK_PROGRAM}.j2',
        _RollbackParams(
            cluster=conventions.CLUSTER_NAME,
            machines_root=MACHINES,
            unit_template=UNIT_TEMPLATE,
            rootfs=ROOTFS,
            marker_suffix=MARKER_SUFFIX,
            superseded_suffix=SUPERSEDED_SUFFIX,
            rejected_suffix=REJECTED_SUFFIX,
        ),
    )


def watchdog_unit() -> str:
    """The watchdog's unit, vendored: what it says about itself is upstream's."""
    return templates.load(persistence.TEMPLATE_PACKAGE, f'templates/{WATCHDOG_UNIT}')


def watchdog_worker() -> str:
    """The watchdog itself, vendored beside its unit."""
    return templates.load(persistence.TEMPLATE_PACKAGE, f'templates/{WATCHDOG_WORKER}')


class NspawnRuntime(Component):
    """The framework the machines run on: the boot chain's half of it, and the watchdog."""

    #: What this layer requires of the device's package set, as one constant the
    #: gateway folds into the union it hands the persistence layer. The runtime
    #: needs the tooling that boots a directory as a machine and the
    #: name-service module that resolves the machines it started; the push needs
    #: `skopeo` and `umoci` on the device's path, because the device pulls and
    #: unpacks its own root filesystems and the provider that drives it declares
    #: no packages of its own. The set is installed in one transaction, which is
    #: why the first two being version-locked to the base system is the
    #: mechanism's problem rather than this constant's.
    REQUIRED_PACKAGES: ClassVar[tuple[str, ...]] = ('systemd-container', 'libnss-mymachines', 'skopeo', 'umoci')

    def __init__(
        self,
        name: str,
        *,
        mechanism: DevicePersistence,
        machines: Sequence[Machine],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        """Declare the runtime, with `machines` as the set `40-machines.sh` converges.

        The set arrives as data rather than as the workload components, which is
        what keeps the two sides acyclic: the gateway builds a `Machine` and a
        `Container` from the same declaration, so a stamped set cannot name a
        file no resource declares, and neither component has to exist before the
        other.
        """
        super().__init__(name, opts=opts)

        self.skeleton: DeviceFile = mechanism.skeleton_dir(SKELETON, opts=self.child_opts())
        # The settings converger before the machine converger, in the order the
        # boot chain runs them and a hook re-runs them.
        self.nspawn_units: DeviceFile = mechanism.on_boot_script(
            NSPAWN_UNITS_SCRIPT, nspawn_units_script(), opts=self.child_opts()
        )
        self.machines: DeviceFile = mechanism.on_boot_script(
            MACHINES_SCRIPT, machines_script(machines), opts=self.child_opts()
        )
        # The rollback the health gate takes when a machine does not come up. It
        # is a converger of nothing, so it has no hook of its own; what it needs
        # is to be on the device before a hook can reach for it.
        self.rollback: DeviceFile = mechanism.executable(ROLLBACK_PROGRAM, rollback_program(), opts=self.child_opts())
        self.watchdog_worker: DeviceFile = mechanism.executable(
            WATCHDOG_WORKER, watchdog_worker(), opts=self.child_opts()
        )
        self.watchdog: DeviceFile = mechanism.unit(WATCHDOG_UNIT, watchdog_unit(), opts=self.child_opts())

        self.register_outputs({})

    @property
    def convergers(self) -> tuple[pulumi.Resource, ...]:
        """What has to be on the device before a machine's first file lands.

        Every file of a machine runs the two scripts as its hook and may reach
        for the rollback, and a hook that runs a program the device has not been
        given fails its own apply.
        """
        return (self.skeleton, self.nspawn_units, self.machines, self.rollback)

    def hook(self, machine: str, path: str) -> str:
        """The post-apply hook one file of `machine` runs once it lands."""
        return machine_hook(machine, path)
