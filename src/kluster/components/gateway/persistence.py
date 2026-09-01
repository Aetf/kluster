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
    changed.
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
instead of converging it.

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

**New device automation is a unit plus an executable unless it manipulates
systemd's own configuration**, in which case it is an on_boot script; the rule
and its reasons are physical/gateway.md §1.2.
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
    'BIN_DIR',
    'DIRECTORY_MODE',
    'DPKG_DIR',
    'FILE_MODE',
    'LIVE_UNIT_DIR',
    'PACKAGES_SCRIPT',
    'SCRIPT_MODE',
    'SKELETON',
    'TEMPLATE_PACKAGE',
    'UDM_BOOT_UNIT',
    'UNITS_SCRIPT',
    'UNIT_SOURCE_DIR',
    'UNIT_SUFFIX',
    'DevicePersistence',
    'executable_hook',
    'executable_path',
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
#: The three paths are unpacked from that same tuple rather than spelled again,
#: so a directory added to the skeleton without a name to reach it by fails
#: here instead of on the device.
SKELETON = ('bin', 'dpkg', 'units')
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
    """What `20-units.sh.j2` reads: the two directories it converges between."""

    cluster: str
    unit_source_dir: str
    live_unit_dir: str


def packages_script(packages: Sequence[str]) -> str:
    """The boot-chain script that reinstalls what a firmware update wiped.

    The set is data and everything else is mechanism: one transaction so that
    version-locked packages resolve together, an offline cache refreshed from
    what apt downloaded on the boot it succeeded, and that cache as the fallback
    for the boot where apt is unreachable.

    The set is sorted and deduplicated here, so the file the device holds is a
    function of what the estate requires rather than of the order the callers
    happened to state it in, and each name is quoted for the shell that reads
    the array — a package name is a caller's string, and one carrying a space
    would otherwise become two entries the device cannot install.
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

    It never restarts `udm-boot.service`, which is the oneshot running it, and
    it mirrors no deletion: the live unit directory holds units that are not
    this program's, and the ones that are are retired by `unit_hook`.
    """
    return templates.render(
        TEMPLATE_PACKAGE,
        f'templates/{UNITS_SCRIPT}.j2',
        _UnitsParams(
            cluster=conventions.CLUSTER_NAME,
            unit_source_dir=UNIT_SOURCE_DIR,
            live_unit_dir=LIVE_UNIT_DIR,
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
        """
        return self._declare(
            'bin',
            name,
            path=executable_path(name),
            content=content,
            mode=SCRIPT_MODE,
            hook=None,
            opts=opts,
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
            after=(self.units,),
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
