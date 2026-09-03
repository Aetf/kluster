"""The device's persistence mechanism: what re-establishes customization at boot.

A firmware update wipes everything the device runs except `/data`, so every
customization of this machine is a file under `/data` plus something that puts
it back into effect with no Pulumi reachable. That something is the boot chain,
and this module owns it:

-   **`udm-boot.service`**, vendored from upstream and pinned in its own
    header. It is a oneshot that runs every file in `/data/on_boot.d` in
    numeric order, and it is delivered as a *unit source* under the custom root
    like any other unit, so a device whose `/etc` was wiped is recovered by one
    manual copy of a file it already holds (physical/gateway.md §1.2).
-   **`10-packages.sh`**, which reinstalls the packages a firmware update took
    away, in one transaction, and keeps an offline deb cache for the boot where
    apt is unreachable. Which packages is data (`packages=`); everything else
    about how they are installed is mechanism and stays in the template.
-   **`20-units.sh`**, which converges the unit sources under the custom root
    into `/etc/systemd/system`, enables them, and restarts the ones whose file
    changed. It converges their drop-in directories too, where a statement lives
    that belongs to one unit rather than to the file it is written in.
-   **The skeleton of the custom root** — `bin/`, `dpkg/`, `units/`.

**The layers above reach the mechanism through methods here, and the resource
they get back is theirs.** Each call is one device resource at a path this
module decides, with the mode and the post-apply hook that path implies, parented
to the component that asked for it: the path and hook discipline belong to this
layer, the resource belongs to the layer that needs it. That is why they are
methods returning a child rather than constructor parameters — a caller would
otherwise have to re-learn the path rules, or hand its content down to a
component that has no idea what it is.

**The package list is the exception, and is constructor data.**
`10-packages.sh` is one rendered file whose content has to be complete when the
resource is created, so accumulating it by method call would mean rendering a
file before its callers exist. Each consumer publishes what it requires as a
constant and `Gateway` passes the union.

**A hook is one command that runs after a write and after a delete**, so where
the two mean different things the command asks which happened by looking for
the file. That is how a unit is retired by the same apply that stops declaring
it: the source is gone, so the hook disables the unit and removes the live copy
instead of converging it, and a drop-in comes off the unit it amended the same
way.

**`dpkg/` is `10-packages.sh`'s alone.** The cache is refreshed by replacing the
whole directory in one rename, so any file Pulumi wrote in it would be deleted
by the next refresh and reported as drift forever. This layer declares that the
directory exists and never what is in it.

**A directory is a resource, not a file that stands for one.** `skeleton_dir`
declares a `DeviceDirectory`, whose existence, mode and ownership are compared
against the device like a file's content is — so a directory somebody removed by
hand is drift the next preview sees. Nothing is declared about the contents,
which is what `dpkg/` requires and what makes the method serve any directory a
layer fills at runtime; the delete that stops declaring one takes it away only
while it is empty.

**One directory is the exception, and it is the one no layer chooses**: a
`<unit>.d`. Its name is decided by the unit being amended rather than by a
caller, it exists exactly while a drop-in in it does, and two components
amending one unit would otherwise have to agree on who declares it. So it is
created by the delivery of the first drop-in in it and removed by the delete of
the last (`dropin_hook`), and nothing about it is ever compared against the
device.

**New device automation is a unit plus an executable unless it manipulates
systemd's own configuration**, in which case it is an on_boot script; the rule
and its reasons are physical/gateway.md §1.2. What that automation cannot state
in a unit of its own — because the unit running it is systemd's, not this
program's — it states in a drop-in.
"""

from __future__ import annotations

import shlex
from collections.abc import Sequence
from dataclasses import dataclass
from typing import final

import pulumi

from kluster import conventions
from kluster.lib import templates
from kluster.providers.device_files.provider import Connection, DeviceDirectory, DeviceFile
from putils import Component

__all__ = (
    'BIN',
    'BIN_DIR',
    'DIRECTORY_MODE',
    'DPKG',
    'DPKG_DIR',
    'DROPIN_DIR_SUFFIX',
    'DROPIN_SUFFIX',
    'FILE_MODE',
    'LIVE_UNIT_DIR',
    'PACKAGES_SCRIPT',
    'SCRIPT_MODE',
    'SKELETON',
    'TEMPLATE_PACKAGE',
    'UDM_BOOT_UNIT',
    'UNITS',
    'UNITS_SCRIPT',
    'UNIT_SOURCE_DIR',
    'UNIT_SUFFIX',
    'DevicePersistence',
    'dropin_dir',
    'dropin_hook',
    'dropin_source',
    'executable_hook',
    'executable_path',
    'live_dropin_dir',
    'on_boot_hook',
    'on_boot_path',
    'packages_script',
    'skeleton_path',
    'udm_boot_unit',
    'unit_hook',
    'unit_source',
    'units_script',
)

#: The package `importlib.resources` resolves the `templates/` directory
#: against. Stated here rather than imported from the modules that declare the
#: services, because the layer underneath them depends on nothing above it.
TEMPLATE_PACKAGE = 'kluster.components.gateway'

# ---------------------------------------------------------------------------
# Where the mechanism lives on the device
# ---------------------------------------------------------------------------

#: The custom root's skeleton: executables, the offline deb cache, and the unit
#: sources `20-units.sh` converges. Every other directory under the custom root
#: belongs to a layer above and arrives through `skeleton_dir`.
#:
#: The names and the three paths are unpacked from that same tuple rather than
#: spelled again, so a directory added to the skeleton without a name to reach
#: it by fails here instead of on the device. The names are what index
#: `DevicePersistence.skeleton`, which is how a file declares that it waits on
#: the directory it lands in.
SKELETON = ('bin', 'dpkg', 'units')
BIN, DPKG, UNITS = SKELETON
BIN_DIR, DPKG_DIR, UNIT_SOURCE_DIR = (f'{conventions.gateway.CUSTOM_ROOT}/{name}' for name in SKELETON)

#: Where systemd reads the units `20-units.sh` installs, which is off `/data`
#: and therefore what a firmware update takes away.
LIVE_UNIT_DIR = '/etc/systemd/system'

#: The three files this layer is. The numbers are the boot order: packages
#: first, because everything after them may need what they install; the unit
#: store next, because a converger that installs units cannot be one.
PACKAGES_SCRIPT = '10-packages.sh'
UNITS_SCRIPT = '20-units.sh'
UDM_BOOT_UNIT = 'udm-boot.service'

#: The unit kind `20-units.sh` converges, which is the one glob it walks. A
#: source of any other kind would land on the device and be installed by
#: nothing, so `unit` refuses it rather than delivering a file with no effect.
UNIT_SUFFIX = '.service'

#: What a drop-in is, in the two halves systemd reads it by: a directory named
#: for the unit it amends, and inside it the one file suffix systemd opens. Both
#: are systemd's spelling rather than this program's choice, and the converger
#: walks the source directory by the same pair.
DROPIN_DIR_SUFFIX = '.d'
DROPIN_SUFFIX = '.conf'

#: A script and an executable are run; a unit file is read; a directory is
#: entered and written in, which is the same bit a script needs for a different
#: reason and so is a mode of its own.
SCRIPT_MODE = '0755'
FILE_MODE = '0644'
DIRECTORY_MODE = '0755'


def on_boot_path(name: str) -> str:
    """Where one script of the boot chain sits. The directory is udm-boot's."""
    return f'{conventions.gateway.ON_BOOT_D}/{name}'


def executable_path(name: str) -> str:
    """Where one executable sits. Neighbours placed by hand are left alone."""
    return f'{BIN_DIR}/{name}'


def unit_source(name: str) -> str:
    """Where one unit's source sits, before `20-units.sh` installs it."""
    return f'{UNIT_SOURCE_DIR}/{name}'


def dropin_dir(unit: str) -> str:
    """Where one unit's drop-ins sit, beside the unit sources they amend."""
    return f'{unit_source(unit)}{DROPIN_DIR_SUFFIX}'


def dropin_source(unit: str, name: str) -> str:
    """Where one drop-in sits, before `20-units.sh` installs it."""
    return f'{dropin_dir(unit)}/{name}'


def live_dropin_dir(unit: str) -> str:
    """Where systemd reads one unit's drop-ins, which is off `/data`."""
    return f'{LIVE_UNIT_DIR}/{unit}{DROPIN_DIR_SUFFIX}'


def skeleton_path(name: str) -> str:
    """One directory of the custom root, by its name under that root."""
    return f'{conventions.gateway.CUSTOM_ROOT}/{name}'


def on_boot_hook(name: str) -> str:
    """Run the script that just landed, the way udm-boot runs it at boot.

    Guarded, because the same command runs after the delete: a script this
    program no longer declares is gone from the device, and the hook has
    nothing left to converge.
    """
    path = shlex.quote(on_boot_path(name))
    return f'if [ -x {path} ]; then {path}; fi'


def executable_hook(name: str) -> str:
    """Run one executable of `bin/`, for a file that names it as its hook.

    An executable is delivered with no hook of its own (`executable`), but a
    file another component declares may need one run once it lands — which is
    how a converger's boot path and its push path stay one program. The guard
    is `on_boot_hook`'s: the same command runs after the delete, and by then
    the program may be gone with the resource that declared it.
    """
    path = shlex.quote(executable_path(name))
    return f'if [ -x {path} ]; then {path}; fi'


def unit_hook(name: str) -> str:
    """Converge the unit, or retire it — whichever the delivery just meant.

    A unit's source landing means the store is out of date, which is
    `20-units.sh`'s whole job. A unit's source *gone* means this program has
    stopped declaring the unit, and the same session that removed the source
    disables the unit and removes the live copy — otherwise a retired unit would
    keep running until somebody noticed it on the device.
    """
    source = shlex.quote(unit_source(name))
    live = shlex.quote(f'{LIVE_UNIT_DIR}/{name}')
    unit = shlex.quote(name)
    return (
        f'if [ -e {source} ]; then {on_boot_hook(UNITS_SCRIPT)}; '
        f'else systemctl disable --now {unit} || true; rm -f {live}; systemctl daemon-reload; fi'
    )


def dropin_hook(unit: str, name: str) -> str:
    """Converge the drop-in, or take it back off the unit it amended.

    The write branch is the unit's: `20-units.sh` is what installs a drop-in,
    and it reloads systemd when one lands. The delete branch cannot be, because
    the converger only ever walks sources — a drop-in this program has stopped
    declaring has no source left to be noticed by. So the delete removes the
    live copy itself, takes the directory with it once it is the last one there,
    and reloads: what the unit loses is the statement, not the unit.

    The source directory goes the same way, and for the same reason it was never
    a resource: it is `mkdir -p`'d by the delivery of the first drop-in in it,
    so nothing else would ever take it away.
    """
    source = shlex.quote(dropin_source(unit, name))
    source_dir = shlex.quote(dropin_dir(unit))
    live = shlex.quote(f'{live_dropin_dir(unit)}/{name}')
    live_dir = shlex.quote(live_dropin_dir(unit))
    return (
        f'if [ -e {source} ]; then {on_boot_hook(UNITS_SCRIPT)}; '
        f'else rm -f {live}; rmdir {live_dir} {source_dir} 2>/dev/null || true; systemctl daemon-reload; fi'
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


@final
@dataclass(frozen=True)
class _PackagesParams:
    """What `10-packages.sh.j2` reads: the set, and where the cache lives."""

    cluster: str
    packages: tuple[str, ...]
    cache: str


@final
@dataclass(frozen=True)
class _UnitsParams:
    """What `20-units.sh.j2` reads: the two directories it converges between.

    The three suffixes come with them, because the globs the script walks and
    the kinds `unit` and `dropin` accept are one decision: a source this
    layer hands over is a source that script picks up.
    """

    cluster: str
    unit_source_dir: str
    live_unit_dir: str
    unit_suffix: str
    dropin_dir_suffix: str
    dropin_suffix: str


def packages_script(packages: Sequence[str]) -> str:
    """The boot-chain script that reinstalls what a firmware update wiped.

    The set is data and everything else is mechanism: one transaction so that
    version-locked packages resolve together, an offline cache refreshed from
    what apt downloaded on the boot it succeeded, and that cache as the fallback
    for the boot where apt is unreachable.

    The set is sorted and deduplicated here, so the file the device holds is a
    function of what the installation requires rather than of the order the
    callers happened to state it in, and each name is quoted for the shell that
    reads the array — a package name is a caller's string, and one carrying a
    space would otherwise become two entries the device cannot install.
    """
    return templates.render(
        TEMPLATE_PACKAGE,
        f'templates/{PACKAGES_SCRIPT}.j2',
        _PackagesParams(
            cluster=conventions.CLUSTER_NAME,
            packages=tuple(shlex.quote(package) for package in sorted(set(packages))),
            cache=DPKG_DIR,
        ),
    )


def units_script() -> str:
    """The boot-chain script that installs, enables and restarts the units.

    It never restarts `udm-boot.service`, which is the oneshot running it.

    The drop-in directories are converged before the unit files, so that a unit
    it then restarts starts with the statements its drop-ins carry. Which of
    the directories it mirrors deletions into and which it only adds to is one
    rule for the whole boot chain, stated in physical/gateway.md §1.2: the unit
    store is shared with the firmware and a unit is therefore retired from the
    other side, by `unit_hook`, while a `<unit>.d` directory this layer has a
    source for is wholly its own.
    """
    return templates.render(
        TEMPLATE_PACKAGE,
        f'templates/{UNITS_SCRIPT}.j2',
        _UnitsParams(
            cluster=conventions.CLUSTER_NAME,
            unit_source_dir=UNIT_SOURCE_DIR,
            live_unit_dir=LIVE_UNIT_DIR,
            unit_suffix=UNIT_SUFFIX,
            dropin_dir_suffix=DROPIN_DIR_SUFFIX,
            dropin_suffix=DROPIN_SUFFIX,
        ),
    )


def udm_boot_unit() -> str:
    """The vendored oneshot that runs the boot chain, exactly as upstream has it.

    Not rendered: the pin in its header says which upstream commit these bytes
    are, and a file assembled from parameters could not carry that claim.
    """
    return templates.load(TEMPLATE_PACKAGE, f'templates/{UDM_BOOT_UNIT}')


class DevicePersistence(Component):
    """The boot chain, the custom root's skeleton, and the way in for the layers above."""

    def __init__(
        self,
        name: str,
        *,
        connection: Connection,
        packages: Sequence[str],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        """Declare the mechanism, with `packages` as the set the device must hold.

        `packages` is the union of what the layers above require. An empty set
        is refused rather than rendered: the script expands the set into one
        `apt-get install`, and an empty expansion under `set -u` fails the boot
        it was supposed to repair. The refusal comes before the registration, so
        a rejected argument leaves no half-built component behind for the parent
        backstop to blame the next resource on (framework/pulumi.md §1.3).
        """
        if not packages:
            raise ValueError(
                'DevicePersistence needs at least one package: the device holds none of its own, and an '
                'empty set renders a script that fails on the boot it exists to repair'
            )
        super().__init__(name, opts=opts)
        self._connection: Connection = connection

        # The unit converger comes first because every unit delivered through
        # this layer runs it as its hook, and a hook that runs a script the
        # device has not been given fails its own apply.
        self.units: DeviceFile = self.on_boot_script(UNITS_SCRIPT, units_script(), opts=self.child_opts())
        self.packages: DeviceFile = self.on_boot_script(
            PACKAGES_SCRIPT, packages_script(packages), opts=self.child_opts()
        )
        self.skeleton: dict[str, DeviceDirectory] = {
            directory: self.skeleton_dir(directory, opts=self.child_opts()) for directory in SKELETON
        }
        # The anchor of the chain, delivered as a unit source like any other:
        # what makes a wiped `/etc` recoverable is that the device already holds
        # the file, and `20-units.sh` is what puts it back.
        self.udm_boot: DeviceFile = self.unit(UDM_BOOT_UNIT, udm_boot_unit(), opts=self.child_opts())

        self.register_outputs({})

    def on_boot_script(self, name: str, content: pulumi.Input[str], *, opts: pulumi.ResourceOptions) -> DeviceFile:
        """One script of the boot chain, run once it lands.

        The delivery runs it, so the path that recovers a device after a
        firmware update is the path every apply exercises. `name` carries the
        numeric prefix that orders it, and `opts` is the calling component's
        `child_opts()`.
        """
        return self._declare(
            'on-boot',
            name,
            path=on_boot_path(name),
            content=content,
            mode=SCRIPT_MODE,
            hook=on_boot_hook(name),
            opts=opts,
        )

    def executable(self, name: str, content: pulumi.Input[str], *, opts: pulumi.ResourceOptions) -> DeviceFile:
        """One executable under `bin/`, delivered and nothing else.

        No hook: what runs it is a unit, per this layer's local rule, and
        installing a program is not an event anything on the device has to be
        told about. Only the file named is managed, so an executable placed by
        hand beside it is left alone.

        It waits on the directory it lands in, which is the edge a destroy
        needs: Pulumi deletes in reverse dependency order, and a directory
        deleted before the files in it refuses to go and fails the run
        (`DeviceDirectory`).
        """
        return self._declare(
            'bin',
            name,
            path=executable_path(name),
            content=content,
            mode=SCRIPT_MODE,
            hook=None,
            opts=opts,
            after=(self.skeleton[BIN],),
        )

    def unit(self, name: str, content: pulumi.Input[str], *, opts: pulumi.ResourceOptions) -> DeviceFile:
        """One systemd unit, converged into the live store — and retired from it.

        The source lands under the custom root, where a firmware update leaves
        it, and the hook is `20-units.sh`: installed, enabled, and restarted if
        its file changed. The delete of this resource is what retires the unit,
        in the same session (`unit_hook`).

        Only a `.service` is accepted, because that is the one glob the
        converger walks: a timer or a mount delivered here would sit on the
        device installed by nothing, which is the one failure this mechanism
        cannot report. Extending the kinds is a change to `20-units.sh` and to
        `UNIT_SUFFIX` together.

        It waits on the converger for the reason above and on the directory it
        lands in for the reason `executable` does: a destroy deletes in reverse
        dependency order, and the directory refuses to go while a unit source is
        still in it.
        """
        if not name.endswith(UNIT_SUFFIX):
            raise ValueError(
                f'{name!r} is not a {UNIT_SUFFIX} unit, and {UNITS_SCRIPT} converges no other kind: '
                f'a source it does not walk would land on the device and be installed by nothing'
            )
        return self._declare(
            'unit',
            name,
            path=unit_source(name),
            content=content,
            mode=FILE_MODE,
            hook=unit_hook(name),
            opts=opts,
            after=(self.units, self.skeleton[UNITS]),
        )

    def dropin(self, unit: str, name: str, content: pulumi.Input[str], *, opts: pulumi.ResourceOptions) -> DeviceFile:
        """One drop-in on a unit, converged into the live store — and taken back off it.

        **A drop-in amends a unit; it does not declare one.** That is what makes
        it the answer where `unit` is not: the unit being amended may be one this
        program never writes, and `systemd-nspawn@<machine>.service` — systemd's
        own template, instanced per machine — is exactly such a unit. What a
        machine cannot say for itself in a settings file is said here instead.

        The source lands in `<unit>.d` under the unit sources, where a firmware
        update leaves it, and the hook is `20-units.sh`: installed, and systemd
        reloaded so that the statement applies to the unit as it stands. Nothing
        is enabled or started from a drop-in, because a drop-in directory is not
        a unit; a statement that only takes effect at the next start is picked up
        by whatever restarts that unit. The delete of this resource is what takes
        the statement off again, in the same session (`dropin_hook`).

        Both suffixes are held to what systemd reads, and to what the converger
        therefore walks: a drop-in directory for a unit of another kind, or a
        file inside one that is not a `.conf`, would land on the device and be
        read by nobody.

        It waits on the converger and on the unit source directory for the
        reasons `unit` does. The `<unit>.d` directory itself is no resource,
        which is this module's one exception to declaring a directory and is
        stated with the rule above.
        """
        if not unit.endswith(UNIT_SUFFIX):
            raise ValueError(
                f'{unit!r} is not a {UNIT_SUFFIX} unit, and {UNITS_SCRIPT} converges the drop-ins of no other '
                f'kind: a directory it does not walk would land on the device and be read by nothing'
            )
        if not name.endswith(DROPIN_SUFFIX):
            raise ValueError(
                f'{name!r} is not a {DROPIN_SUFFIX} drop-in, and systemd opens no other file in a '
                f'{DROPIN_DIR_SUFFIX} directory: it would land on the device and be read by nothing'
            )
        return self._declare(
            'dropin',
            f'{unit}{DROPIN_DIR_SUFFIX}/{name}',
            path=dropin_source(unit, name),
            content=content,
            mode=FILE_MODE,
            hook=dropin_hook(unit, name),
            opts=opts,
            after=(self.units, self.skeleton[UNITS]),
        )

    def skeleton_dir(self, name: str, *, opts: pulumi.ResourceOptions) -> DeviceDirectory:
        """One directory under the custom root, whose contents are the caller's.

        What is declared is the directory itself and nothing inside it. That is
        what a layer needs for a directory it fills at runtime — the offline deb
        cache, a machine's state — rather than one whose every file is a
        resource, and it is why a directory this program stops declaring is
        removed only while it is empty.

        No hook: a directory appearing is not an event anything on the device has
        to be told about, and the layer that fills it is the one that knows
        otherwise.
        """
        return DeviceDirectory(
            f'{self.pulumi_resource_name}-skeleton-{name}',
            connection=self._connection,
            path=skeleton_path(name),
            mode=DIRECTORY_MODE,
            owner=conventions.gateway.SSH_USER,
            opts=opts,
        )

    def _declare(
        self,
        kind: str,
        name: str,
        *,
        path: str,
        content: pulumi.Input[str],
        mode: str,
        hook: str | None,
        opts: pulumi.ResourceOptions,
        after: Sequence[pulumi.Resource] = (),
    ) -> DeviceFile:
        """One file of the mechanism, however it was asked for.

        The kind and the file's own name — suffix included — are what name the
        resource, exactly as they are what decide the path. So two callers
        asking for one path collide on one Pulumi name and are refused at
        declaration time, while two files that merely share a stem stay two
        resources.
        """
        return DeviceFile(
            f'{self.pulumi_resource_name}-{kind}-{name}',
            connection=self._connection,
            path=path,
            content=content,
            mode=mode,
            owner=conventions.gateway.SSH_USER,
            hook=hook,
            opts=pulumi.ResourceOptions.merge(pulumi.ResourceOptions(depends_on=list(after)), opts),
        )
