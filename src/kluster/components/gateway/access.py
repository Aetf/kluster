"""Who may open a shell on the device, and what keeps that true after an update.

The device has one account and it is reached by public key. The keys this
program declares are files under the custom root — where a firmware update
leaves them — and a converger appends any that root's `authorized_keys` does
not already hold.

**Append-only, and that is the design rather than a shortcut.** The file also
holds keys nobody declared: an operator's own, one pasted in during a recovery
where this program was not reachable. Mirroring the declaration onto it would
delete those, which on this machine means locking out the person holding the
only other way in. So a key is added when it is absent and nothing is ever
removed; retiring one is an act on the device, not a delete of a resource here.

**A key is a file so that adding one is not editing one.** Every declared key
is its own resource under `authorized_keys.d/`, which is what makes two keys
independent — one can move while the other does not — and what lets the
converger be a loop over a directory instead of a renderer of a file it would
then have to own.

Like the routing configuration, the pieces are a unit plus an executable rather
than a script in the boot chain (physical/gateway.md §1.2), and the executable
is also each key file's post-apply hook: a key declared during an apply is
usable when that apply returns, not at the next boot.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import final

import pulumi

from kluster import conventions
from kluster.components.gateway.persistence import (
    FILE_MODE,
    TEMPLATE_PACKAGE,
    DevicePersistence,
    executable_hook,
    executable_path,
    skeleton_path,
)
from kluster.lib import templates
from kluster.providers.device_files.provider import Connection, DeviceFile
from putils import Component

__all__ = (
    'ACCOUNT_HOME',
    'AUTHORIZED_KEYS',
    'CONVERGER',
    'CONVERGER_UNIT',
    'KEY_DIRECTORY',
    'KEY_SUFFIX',
    'AuthorizedKeys',
    'PublicKey',
    'converger_hook',
    'converger_script',
    'converger_unit',
    'key_path',
)

#: The directory the declared keys are delivered into, as a name under the
#: custom root: the layer below decides what that root is.
KEY_DIRECTORY = 'authorized_keys.d'

#: What the converger's loop matches, and therefore what a key file must be
#: called for the device to read it.
KEY_SUFFIX = '.pub'

#: The home of the account this program configures the device as
#: (`conventions.gateway.SSH_USER`), spelled out rather than derived: `root`'s
#: home is the one that is not under `/home`, and a rule that got that right by
#: accident would be wrong for the next account.
ACCOUNT_HOME = '/root'

#: The file the device's ssh daemon actually reads. It is off `/data`, so a
#: firmware update takes it away and the converger is what puts the estate's
#: key back.
AUTHORIZED_KEYS = f'{ACCOUNT_HOME}/.ssh/authorized_keys'

#: The converger, and the unit that runs it at boot.
CONVERGER = 'authorized-keys.sh'
CONVERGER_UNIT = 'authorized-keys.service'


@final
@dataclass(frozen=True)
class PublicKey:
    """One key that may open a session on the device, under a name of its own.

    `name` is what the key is called on the device and in a preview, so it says
    who holds the private half rather than what the key is. `key` is the
    `authorized_keys` line itself — the whole line, comment included, because
    the converger compares lines and a line that differs only in its comment is
    a second key as far as the file is concerned.
    """

    name: str
    key: str


@final
@dataclass(frozen=True)
class _ConvergerParams:
    """What `authorized-keys.sh.j2` reads: where the keys are, and where they go."""

    cluster: str
    key_dir: str
    suffix: str
    authorized_keys: str


@final
@dataclass(frozen=True)
class _UnitParams:
    """What `authorized-keys.service.j2` reads: what it runs, and what must exist."""

    cluster: str
    key_dir: str
    executable: str


def key_path(name: str) -> str:
    """Where one declared key sits, before the converger appends it."""
    return f'{skeleton_path(KEY_DIRECTORY)}/{name}{KEY_SUFFIX}'


def converger_script() -> str:
    """The executable that appends the declared keys, and removes nothing.

    Written in POSIX shell, for a device whose interpreters are whatever its
    firmware ships. It is idempotent by comparing whole lines: a key already in
    the file — declared here or added by hand — is left where it is.

    **A line is only whole if the one before it ended.** A file whose last line
    has no newline would otherwise take the appended key onto the end of it,
    making two keys into one string the daemon accepts as neither — including,
    since the file holds keys nobody here declared, somebody's only other way
    in. So the newline is ensured before anything is appended.
    """
    return templates.render(
        TEMPLATE_PACKAGE,
        f'templates/{CONVERGER}.j2',
        _ConvergerParams(
            cluster=conventions.CLUSTER_NAME,
            key_dir=skeleton_path(KEY_DIRECTORY),
            suffix=KEY_SUFFIX,
            authorized_keys=AUTHORIZED_KEYS,
        ),
    )


def converger_unit() -> str:
    """The oneshot that runs the converger at boot.

    `RemainAfterExit` for the reason every oneshot delivered through the
    persistence layer carries it: the unit converger starts what it finds
    inactive, and a oneshot without it is inactive as soon as it succeeds.
    """
    return templates.render(
        TEMPLATE_PACKAGE,
        f'templates/{CONVERGER_UNIT}.j2',
        _UnitParams(
            cluster=conventions.CLUSTER_NAME,
            key_dir=skeleton_path(KEY_DIRECTORY),
            executable=executable_path(CONVERGER),
        ),
    )


def converger_hook() -> str:
    """Append whatever key just landed, without waiting for a boot.

    Guarded as layer 1 guards a hook that runs an executable. A key file's
    delete is not a revocation — the converger removes nothing — so what this
    does after one is exactly nothing.
    """
    return executable_hook(CONVERGER)


class AuthorizedKeys(Component):
    """The keys that open the device, and the converger that keeps them installed."""

    def __init__(
        self,
        name: str,
        *,
        connection: Connection,
        mechanism: DevicePersistence,
        keys: Sequence[PublicKey],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        """Declare `keys` on the device, with the converger that installs them.

        Every refusal below comes before the registration, so a rejected
        argument leaves no half-built component behind for the parent backstop
        to blame the next resource on (framework/pulumi.md §1.3):

        -   **No key at all is refused.** The converger removes nothing, so an
            empty set would deliver a component that cannot fail and cannot
            help — while the machine it configures is one whose door this
            program is otherwise the only thing holding open.
        -   **A key that is not one line is refused.** The converger decides
            "already present" by matching whole lines, and a file holding two
            of them would be reported present as soon as either was, so the
            other would never be appended.
        -   **Two keys under one name are refused**, because the name is the
            file: the second would silently replace the first on the device.
        """
        seen: set[str] = set()
        for key in keys:
            if len(key.key.strip().splitlines()) != 1:
                raise ValueError(
                    f'{key.name!r} is not a single authorized_keys line: the converger matches whole lines, '
                    f'so only the first of several would ever be reported present'
                )
            if key.name in seen:
                raise ValueError(f'{key.name!r} is declared twice, and the name is the file it lands in')
            seen.add(key.name)
        if not seen:
            raise ValueError(
                'AuthorizedKeys needs at least one key: the converger removes nothing, so an empty set '
                'declares a device this program can no longer open a session on after a firmware update'
            )
        super().__init__(name, opts=opts)

        self.directory: DeviceFile = mechanism.skeleton_dir(KEY_DIRECTORY, opts=self.child_opts())
        self.converger: DeviceFile = mechanism.executable(CONVERGER, converger_script(), opts=self.child_opts())
        # The unit is what runs the converger at boot; it waits for the
        # executable, because installing a unit starts it.
        self.unit: DeviceFile = mechanism.unit(
            CONVERGER_UNIT, converger_unit(), opts=self.child_opts(depends_on=[self.converger])
        )
        # One file per key, each waiting for the directory it lands in and for
        # the hook it will run: a hook that is not on the device yet fails the
        # write that delivered the file.
        after = self.child_opts(depends_on=[self.directory, self.converger])
        self.keys: dict[str, DeviceFile] = {
            key.name: DeviceFile(
                f'{name}-key-{key.name}',
                connection=connection,
                path=key_path(key.name),
                content=f'{key.key.strip()}\n',
                mode=FILE_MODE,
                owner=conventions.gateway.SSH_USER,
                hook=converger_hook(),
                opts=after,
            )
            for key in keys
        }

        self.register_outputs({})
