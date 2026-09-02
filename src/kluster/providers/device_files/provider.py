"""The gw-config dynamic provider: desired-state files on the gateway device.

The device has no API for what matters on it -- routing, the container services,
the scripts that re-establish both after a firmware update -- but it has a proven
convention: files under `/data`, written idempotently, with a command run
afterwards to make whatever reads them notice. This module turns that convention
into three Pulumi resources (architecture.md §5.2):

-   `DeviceFile` is a file: path, content, ownership, mode, and an optional
    hook. Its content lives in state, because a configuration file is small
    and its diff is the reason to have a preview at all.
-   `DeviceDirectory` is a directory: path, mode, ownership, and an optional
    hook. It declares that the directory is there and says nothing about what is
    inside it, which is what a layer needs for a directory something else fills
    at runtime -- and the reason its delete takes the directory away only while
    it is empty.
-   `DeviceArtifact` is a tree too big for state: a container image, the
    manifest digest that pins it, and the directory on the device its root
    filesystem is unpacked into. What state carries is the pin, and what the
    device carries is a marker file beside the tree naming the digest it holds.
    A preview compares two hashes, never megabytes.

**The device pulls its own root filesystems.** The runner hands it the pinned
reference and nothing else: over the same session, the device runs `skopeo copy`
from the registry into a staging OCI layout beside the tree, then
`umoci raw unpack` out of that layout into a staging tree. Neither the bytes nor
the code that understands them passes through the runner -- digest verification
is skopeo's, and whiteout, hard link and extended-attribute semantics are
umoci's. Both have to be on the session's `PATH`: putting them there is the
caller's business, and a device without them fails the pull by name.

**The tree is derived state** -- the push puts it there, the push replaces it,
and the push takes it away -- so it is never edited in place: the unpack lands
in a sibling staging directory and the tree moves into position with two
renames, and no moment passes in which a half-unpacked tree is the one a
container boots. The tree it displaced is left behind until the *next* push
clears it, because at the moment of the swap the container is still running on
it and has not yet been restarted. The staging layout goes as soon as the
unpack has read it, so the image is never held in two forms for longer than the
unpack needs both.

**The digest pins the artifact as published.** The pull is by manifest digest,
so the registry's answer is checked against the pin before anything is written,
and every layer under it is checked against that verified manifest. A pin that
names a multi-platform index resolves through the device's own architecture,
which is the one answer that can be right for the machine unpacking it. The
marker the device keeps records *the pin* -- the manifest's digest, not a
checksum of the tree beside it. A marker is the device's claim about
provenance, not a checksum of its neighbour.

**Every resource here diffs against the device, not only against state.** A file
someone edited on the box shows up as a change in `pulumi preview` without a
refresh, which is what makes this convergence rather than record-keeping. The
cost is that a preview opens a session per resource and fails if the device is
unreachable -- deliberate, since a preview that silently reports "no changes"
about a device it never reached is worse than one that says it could not look.

**A change to any declared input is a change**, whether or not the device already
agrees: an update rewrites bytes the device may already have, which is free and
idempotent. The address dialled is declared and converges the same way -- it is
the same box behind a new address, during a first bring-up reached over the LAN
rather than over the overlay (physical/gateway.md §2.5) -- so the file is
rewritten at the new address rather than moved to it, and nothing is deleted at
the old one.

**The session is the provider's own business, and `check` is what makes it
visible.** The credential that opens the device is read in `configure`, out of
stack configuration, inside the plugin's process: no caller declares it, no
component passes it, and nothing pickles it. So that a reader still sees what it
does, `check` adds two properties to every resource's checked inputs --
`session`, the endpoint and a short digest of the credential, and
`provider_version`, this module's own version. The engine compares checked
inputs, so a rotation and a change to this module's behavior each render as a
property diff no caller declared (rfc-002 §7.4). Neither is a change to the
device: an update whose declared inputs all match re-stamps the resource and
writes nothing. That whole shape is `kluster.providers.configured`, which every
custom provider here shares; what is this module's own is which key holds the
credential and what an endpoint is.

**Only the path is a replacement.** The bytes cannot be at two paths at once, so
a moved file is created before the old one is deleted. Nothing else about a
resource here identifies it, the address least of all: a replacement triggered by
an address would delete, at what is the same device, the file its own create had
just written there.

**Order within an apply is chosen so that a failure is visible.** A file is
written, then its hook runs; a failed hook raises, so the resource is not
recorded -- the bytes may already be on the device, and the retry writes the
same ones, every operation here being idempotent by construction.

An artifact carries a marker as well, and it cannot go last. The gateway's hook
is a script that restarts a container only when a file it reads has changed, and
the marker beside the tree is that file, so a marker written after the hook would
be a change the hook could never see -- the container would learn of a new root
filesystem one deployment late. The marker therefore precedes the hook, and **a
hook that does not succeed withdraws it**, however it failed: the marker is the
claim that this tree came from this pin and that the device has acted on it, and
half of that claim being false makes the whole of it false. What the next preview
sees is a tree of unknown provenance, which is work to do.

A file's content is declared secret on request (`secret=True`), for the files
that carry device credentials, and not by default: a secret input renders as a
hash in a preview, and most of what a device holds is configuration one wants to
read. The pinned host key is deliberately not secret either -- it is a public
key, and a reviewable pin is worth more than a redacted one.

The provider classes carry nothing into state. A dynamic provider is pickled into
every resource it manages, so what these classes serialize to is what state
holds: their own module and class name and an empty bag -- inert, identical for
every resource, and unchanged by a credential rotation. They are revived in a
separate process where each call arrives on a thread with no event loop, so every
operation runs its own loop, opens its own session and closes both. The one seam
a test replaces -- how a session is opened -- is a module-level function looked
up when it is called rather than an attribute captured at construction, which
keeps the pickled provider a constant. There is no second seam: the device does
the pulling, so a test that has replaced the session has replaced everything
that leaves the runner.
"""

from __future__ import annotations

import abc
import asyncio
import shlex
from collections.abc import Coroutine, Mapping
from contextlib import AbstractAsyncContextManager, suppress
from dataclasses import dataclass
from typing import Any, final

import pulumi
import pulumi.dynamic as dynamic

from kluster.providers.configured import (
    STAMPS,
    ConfiguredProvider,
    declared_change,
    has_unknowns,
    is_unknown,
)
from kluster.providers.device_files import ssh

__all__ = (
    'ADDRESS',
    'ARTIFACT_COMPARED',
    'ARTIFACT_DECLARED',
    'DIRECTORY_COMPARED',
    'DIRECTORY_DECLARED',
    'FILE_COMPARED',
    'FILE_DECLARED',
    'LAYOUT_SUFFIX',
    'LAYOUT_TAG',
    'PIN',
    'PRIVATE_KEY_CONFIG',
    'SUPERSEDED_SUFFIX',
    'UNPACKING_SUFFIX',
    'VERSION',
    'Connection',
    'DeviceArtifact',
    'DeviceArtifactProvider',
    'DeviceDirectory',
    'DeviceDirectoryProvider',
    'DeviceFile',
    'DeviceFileProvider',
    'DeviceProvider',
    'DirectoryNotEmpty',
    'HookFailed',
    'MakeDirectoryFailed',
    'PullFailed',
    'RemoveDirectoryFailed',
    'UnpackFailed',
    'endpoint',
    'gone',
    'make_script',
    'marker_path',
    'open_transport',
    'pull_script',
    'purge_script',
    'reference',
    'remove_script',
    'secret_outputs',
    'unpack_script',
)

#: Where the device answers and as whom the session authenticates. These are
#: declared inputs rather than a resource identity: a change to one dials the new
#: address and writes the same file there, and does *not* delete anything at the
#: old one. A session names exactly one device, pinned by its host key rather than
#: by its address, so an address that moved -- the LAN dial of a first bring-up,
#: physical/gateway.md §2.5 -- is the same box, and a replacement's delete would
#: remove the file its own create had just written. A device genuinely swapped
#: out keeps whatever was on it, which is what happens to a box that has been
#: taken off the network anyway.
ADDRESS = ('host', 'port', 'username')

#: The key the device must present. Declared like the address and for the same
#: reason: it is the caller's decision, and a provider has no way to reach the
#: caller's decisions for itself. A public key by nature, and nothing marks it
#: secret on either side of the boundary, so a preview shows it -- which is
#: where a reviewer checks the pin a session will be held to, and a pin nobody
#: can read is a pin nobody reviews. The credential that answers it goes the
#: other way -- `PRIVATE_KEY_CONFIG`, read in `configure` and declared by
#: nobody.
PIN = ('host_key',)

#: What a `DeviceFile` declares beyond its own path.
FILE_DECLARED = ('content', 'mode', 'owner', 'hook', *ADDRESS, *PIN)

#: The same for a `DeviceArtifact`. The digest is the tree's identity, and the
#: repository and tag are only where those bytes were found -- but all three are
#: declared, so moving publication to another registry, or a tag that moved onto
#: a digest the device already holds, is a diff a reviewer sees rather than a
#: silent equivalence. Ownership and mode are declared by neither: what a root
#: filesystem's files belong to and what they may do is the image's statement
#: about itself, and a push that overrode it would be pushing something other
#: than the image the pin names.
ARTIFACT_DECLARED = ('repository', 'tag', 'digest', 'hook', *ADDRESS, *PIN)

#: The same for a `DeviceDirectory`. Nothing about the contents appears here,
#: because nothing about the contents is declared: what the resource states is
#: that the directory exists and who may do what in it.
DIRECTORY_DECLARED = ('mode', 'owner', 'hook', *ADDRESS, *PIN)

#: The stack-configuration key the session's credential is read from
#: (`configured`), which makes this module the only code in the repository that
#: ever holds the device's private key.
PRIVATE_KEY_CONFIG = 'gatewayPrivateKey'

#: This module's version, bumped by hand when an operation's behavior changes
#: (`configured`).
VERSION = '3'

#: What `diff` compares, and the whole of it. Its `olds` is the stored *output*
#: bag while its `news` is the checked *input* bag (rfc-002 §7.5 E7), so a key
#: that only an operation's outs ever carried lives in `olds` alone -- and a
#: provider comparing the two bags wholesale reports a change on every run.
FILE_COMPARED = (*FILE_DECLARED, *STAMPS)
ARTIFACT_COMPARED = (*ARTIFACT_DECLARED, *STAMPS)
DIRECTORY_COMPARED = (*DIRECTORY_DECLARED, *STAMPS)

#: The mode a digest marker is written with. The marker is the provider's own
#: bookkeeping, not part of the image, so it takes a fixed mode.
MARKER_MODE = '0644'

#: Where a tree is assembled before it becomes the live one, and where the tree
#: it displaced waits to be cleared. Both sit beside the tree rather than in a
#: temporary directory, so the renames that swap them stay within one filesystem
#: and are therefore atomic.
UNPACKING_SUFFIX = '.kluster-unpacking'
SUPERSEDED_SUFFIX = '.kluster-superseded'

#: Where the pulled image waits between the two device-side commands, as an OCI
#: layout. Beside the tree for the same reason the other two are: the filesystem
#: holding the tree is the one with room for the image it came from.
LAYOUT_SUFFIX = '.kluster-oci'

#: What the image is called inside that layout. An OCI layout indexes its
#: contents by reference name, and both commands have to agree on one; which
#: name it is means nothing, because the layout holds exactly one image and is
#: deleted as soon as it has been read.
LAYOUT_TAG = 'pinned'


@final
class HookFailed(Exception):
    """A post-apply hook exited non-zero, so the apply did not converge."""

    def __init__(self, command: str, exit_status: int, stderr: str) -> None:
        super().__init__(f'post-apply hook `{command}` exited {exit_status}: {stderr.strip() or "(no output)"}')
        self.command: str = command
        self.exit_status: int = exit_status
        self.stderr: str = stderr


@final
class PullFailed(Exception):
    """The device could not fetch the pinned image, so nothing was unpacked.

    Everything between the device and the registry lands here -- name
    resolution, the outbound connection, the signature policy skopeo reads, a
    digest the registry does not serve -- because all of it is one command's
    exit status and one command's standard error, and the device is the only
    place that knows which it was.
    """

    def __init__(self, image: str, exit_status: int, stderr: str) -> None:
        super().__init__(f'pulling {image} on the device exited {exit_status}: {stderr.strip() or "(no output)"}')
        self.image: str = image
        self.exit_status: int = exit_status
        self.stderr: str = stderr


@final
class UnpackFailed(Exception):
    """The unpack failed before any rename, so the tree the device boots is untouched."""

    def __init__(self, directory: str, exit_status: int, stderr: str) -> None:
        super().__init__(f'unpacking into {directory} exited {exit_status}: {stderr.strip() or "(no output)"}')
        self.directory: str = directory
        self.exit_status: int = exit_status
        self.stderr: str = stderr


@final
class MakeDirectoryFailed(Exception):
    """The device could not create the directory or give it the declared shape."""

    def __init__(self, path: str, exit_status: int, stderr: str) -> None:
        super().__init__(f'making {path} on the device exited {exit_status}: {stderr.strip() or "(no output)"}')
        self.path: str = path
        self.exit_status: int = exit_status
        self.stderr: str = stderr


@final
class RemoveDirectoryFailed(Exception):
    """The device could not remove the directory, for a reason of its own."""

    def __init__(self, path: str, exit_status: int, stderr: str) -> None:
        super().__init__(f'removing {path} on the device exited {exit_status}: {stderr.strip() or "(no output)"}')
        self.path: str = path
        self.exit_status: int = exit_status
        self.stderr: str = stderr


@final
class DirectoryNotEmpty(Exception):
    """A directory this program stops declaring still holds somebody else's files.

    Nothing about the contents was ever declared, so nothing about them may be
    deleted: the resource is the directory, and emptying it would destroy state
    the device -- or a person -- put there. The operation fails instead, and what
    resolves it is a decision on the device rather than a wider delete here.
    """

    def __init__(self, path: str) -> None:
        super().__init__(
            f"refusing to remove {path}: it is not empty, and its contents are not this resource's to delete"
        )
        self.path: str = path


@final
@dataclass(frozen=True)
class Connection:
    """Where and as whom to write, and the key the device must present.

    Everything here is a caller's decision, which is why it travels as resource
    inputs, and none of it is a credential. `host_key` is the pin, and it is
    required: there is no shape of this object that means "trust whatever
    answers". It is a public key and it travels in the clear, so a preview
    shows it (see `PIN`). The credential the session authenticates with is not
    here at all -- it is `PRIVATE_KEY_CONFIG`, read in the provider's own
    process.
    """

    host: pulumi.Input[str]
    host_key: pulumi.Input[str]
    username: pulumi.Input[str] = 'root'
    port: pulumi.Input[int] = 22

    def props(self) -> dict[str, Any]:
        return {
            'host': self.host,
            'port': self.port,
            'username': self.username,
            'host_key': self.host_key,
        }


def open_transport(device: ssh.Device) -> AbstractAsyncContextManager[ssh.Transport]:
    """Open the session an operation runs through.

    The one seam between the provider and a real device: a test replaces this
    module attribute with a fake, and everything above it is the code that runs
    in production.
    """
    return ssh.connect(device)


def reference(repository: str, digest: str) -> str:
    """The image as a pull names it: a repository and the digest that pins it.

    A tag never appears in it. The tag is declared so that moving publication is
    a diff a reviewer sees, but a name somebody else can move is not what a pull
    resolves.
    """
    return f'{repository}@{digest}'


def pull_script(image: str, directory: str) -> str:
    """The command that puts the pinned image beside `directory`, as a layout.

    Its first act is to clear what the last push left: the tree the last swap
    displaced, any staging tree a failed run abandoned, and any layout a failed
    pull abandoned. That is the moment to do it -- reclaiming the space before
    pulling is what keeps a bumped pin from needing room for three copies of one
    root filesystem at once.

    `skopeo copy` is given a `docker://` source carrying the digest, so the
    registry's answer is verified against the pin before a byte is written, and
    an `oci:` destination, which is the only form `umoci` reads. Nothing here
    names a credential or a policy file: the pull is anonymous and skopeo reads
    the device's own `/etc/containers/policy.json`, which is the permissive
    default its package ships.
    """
    layout = shlex.quote(f'{directory}{LAYOUT_SUFFIX}')
    staging = shlex.quote(f'{directory}{UNPACKING_SUFFIX}')
    superseded = shlex.quote(f'{directory}{SUPERSEDED_SUFFIX}')
    source = shlex.quote(f'docker://{image}')
    destination = shlex.quote(f'oci:{directory}{LAYOUT_SUFFIX}:{LAYOUT_TAG}')
    return ' && '.join(
        (
            f'rm -rf {layout} {staging} {superseded}',
            # Making the layout also makes the directory holding it, which is
            # the directory the tree and the staging tree go in: `umoci` creates
            # the tree it unpacks into but not the one above it, and a device
            # that has never held this service has neither.
            f'mkdir -p {layout}',
            f'skopeo copy --quiet {source} {destination}',
        )
    )


def unpack_script(directory: str) -> str:
    """The commands that make `directory` the root filesystem in the layout.

    Unpacked into a sibling staging tree and moved into place with two renames,
    so the tree a container boots is either the previous one or the new one and
    never a partial one. What the swap displaced is left as a sibling: the
    container is still running on those files at this point in the sequence --
    the hook that restarts it has not run yet -- so it is cleared at the start
    of the *next* push instead.

    `umoci raw unpack` rather than `umoci unpack`, because the latter writes an
    OCI *bundle* -- a `config.json` beside a `rootfs/` directory -- and what
    boots here is a root filesystem, not a runtime bundle. The layout goes the
    moment `umoci` has finished reading it, before the swap rather than after
    it, so the image stops occupying the device in two forms at the earliest
    point where deleting one of them is safe.
    """
    quoted_directory = shlex.quote(directory)
    layout = shlex.quote(f'{directory}{LAYOUT_SUFFIX}')
    staging = shlex.quote(f'{directory}{UNPACKING_SUFFIX}')
    superseded = shlex.quote(f'{directory}{SUPERSEDED_SUFFIX}')
    tagged = shlex.quote(f'{directory}{LAYOUT_SUFFIX}:{LAYOUT_TAG}')
    return ' && '.join(
        (
            f'umoci raw unpack --image {tagged} {staging}',
            f'rm -rf {layout}',
            f'if [ -e {quoted_directory} ]; then mv {quoted_directory} {superseded}; fi',
            f'mv {staging} {quoted_directory}',
        )
    )


def purge_script(directory: str) -> str:
    """The command that removes a tree and every sibling the push uses."""
    paths = (
        directory,
        f'{directory}{UNPACKING_SUFFIX}',
        f'{directory}{SUPERSEDED_SUFFIX}',
        f'{directory}{LAYOUT_SUFFIX}',
    )
    return 'rm -rf ' + ' '.join(shlex.quote(path) for path in paths)


def make_script(path: str, mode: str, owner: str | None) -> str:
    """The command that makes `path` a directory of the declared shape.

    Every step is idempotent, which is what lets one script serve the create and
    the update alike: `mkdir -p` accepts a directory that is already there, and
    the mode and the ownership are set rather than adjusted. Ownership is left
    alone where none was declared, exactly as a write does -- a directory whose
    owner this resource has no opinion about keeps whatever the device gave it.
    """
    quoted = shlex.quote(path)
    script = [f'mkdir -p {quoted}', f'chmod {shlex.quote(mode)} {quoted}']
    if owner:
        script.append(f'chown {shlex.quote(owner)} {quoted}')
    return ' && '.join(script)


def remove_script(path: str) -> str:
    """The command that takes `path` away, and only while nothing is in it.

    Three answers rather than two, because the caller has to tell them apart: a
    path that was already gone is a success, a directory with something in it
    exits `ssh.NOT_EMPTY`, and anything else is the device's own failure with
    its own message. `rmdir` would collapse the last two into one status.

    The emptiness test asks about a directory first. `ls -A` on a regular file
    prints that file's own name, so a file left at the declared path would be
    called "not empty" -- a statement about contents it does not have. It falls
    through to `rmdir` instead, which refuses it as not a directory and says so
    in the device's own words.
    """
    quoted = shlex.quote(path)
    return '; '.join(
        (
            f'if [ ! -e {quoted} ]; then exit 0; fi',
            f'if [ -d {quoted} ] && [ -n "$(ls -A {quoted})" ]; then exit {ssh.NOT_EMPTY}; fi',
            f'rmdir {quoted}',
        )
    )


def marker_path(directory: str) -> str:
    """Where the digest of the tree at `directory` is recorded on the device.

    Beside a tree rather than inside it: a file the push wrote into the tree
    would be a file inside the container's root filesystem, and the tree is the
    image, not the push's bookkeeping.
    """
    return f'{directory}.digest'


def secret_outputs(*, secret_content: bool = False) -> list[str]:
    """The properties the engine must keep out of plain state and previews.

    Only a file's content is ever one. The address and the pin are public, and
    the credential is not a property of any resource.
    """
    return ['content'] if secret_content else []


def endpoint(props: Mapping[str, Any]) -> str:
    """Where a property bag says the session goes, as one legible string."""
    return f'{props["username"]}@{props["host"]}:{props["port"]}'


def run_sync[T](coroutine: Coroutine[Any, Any, T]) -> T:
    """Drive one coroutine to completion from a provider's synchronous method.

    Pulumi calls a Python dynamic provider on a gRPC servicer thread that has no
    event loop of its own, so each operation gets a loop, uses it, and closes it.
    Sharing one would be wrong as well as unnecessary: those threads run
    concurrently.
    """
    return asyncio.run(coroutine)


async def run_hook(transport: ssh.Transport, props: Mapping[str, Any]) -> None:
    """Run the post-apply hook, if the resource declares one."""
    hook = props.get('hook')
    if not hook:
        return
    command = str(hook)
    result = await transport.run(command)
    if not result.ok:
        raise HookFailed(command, result.exit_status, result.stderr)


def replacements(olds: Mapping[str, Any], news: Mapping[str, Any], location: str) -> list[str]:
    """The properties whose change means a different resource, not a new value.

    Where the file sits on the device, and nothing else: that is the only change
    the device cannot converge in place, because the bytes have to appear under
    the new path and go from the old one. Everything else about a `DeviceFile` --
    where the device answers included -- is a value the next apply writes.

    A property that is still unknown cannot have been shown to differ, so it is
    never a reason to plan a replacement; the unknown diff that follows says so
    honestly instead.
    """
    moved = not is_unknown(news.get(location)) and olds.get(location) != news.get(location)
    return [location] if moved else []


def _observed_owner(declared: Any, stat: ssh.FileStat) -> str | None:
    """The device's ownership, in the shape the resource declared it."""
    if not declared:
        return None
    return f'{stat.owner}:{stat.group}' if ':' in str(declared) else stat.owner


def _absolute(props: Mapping[str, Any], key: str) -> list[dynamic.CheckFailure]:
    value = props.get(key)
    if value is None or is_unknown(value):
        return []
    if not str(value).startswith('/'):
        return [dynamic.CheckFailure(key, f'must be an absolute path on the device, got {value!r}')]
    return []


def _registry_qualified(props: Mapping[str, Any], key: str) -> list[dynamic.CheckFailure]:
    """Refuse a repository that does not begin with the registry to ask.

    The pull happens on the device, so an unqualified reference would be
    resolved by the device's own short-name configuration -- a default this
    declaration never named, reached over the network, and exactly what a pin
    exists to prevent. The registry world's own rule decides: a first component
    with a dot or a port in it is a host, and `localhost` is one by exception.
    """
    value = props.get(key)
    if value is None or is_unknown(value):
        return []
    first, separator, _ = str(value).partition('/')
    if not separator or not (first == 'localhost' or '.' in first or ':' in first):
        return [dynamic.CheckFailure(key, f'must begin with a registry host, got {value!r}')]
    return []


def _octal(props: Mapping[str, Any], key: str) -> list[dynamic.CheckFailure]:
    value = props.get(key)
    if value is None or is_unknown(value):
        return []
    try:
        _ = int(str(value), 8)
    except ValueError:
        return [dynamic.CheckFailure(key, f'must be an octal mode such as "0644", got {value!r}')]
    return []


def gone() -> dynamic.ReadResult:
    """What a `read` returns for a resource the device no longer has.

    Dropping the identifier is how the engine learns the resource is gone. The
    outputs are an empty bag rather than `None`, and a fresh one each time,
    because the dynamic-provider host writes its own bookkeeping key into
    whatever bag it is handed.
    """
    return dynamic.ReadResult(id_=None, outs={})


class DeviceProvider(ConfiguredProvider, abc.ABC):
    """What the device providers share: the session, and what it makes visible.

    The stateless-provider machinery is `kluster.providers.configured`: the key
    is read in `configure`, nothing is pickled, and `check` stamps the session
    and the version. What this class adds is what those hooks mean for a device
    -- the key is `PRIVATE_KEY_CONFIG`, the endpoint is where a session dials,
    and an update that only re-stamps must still ask the device whether it
    agrees.
    """

    private_key: str

    def _read_credential(self, config: dynamic.Config) -> None:
        self.private_key = str(config.require(PRIVATE_KEY_CONFIG))

    def _credential(self) -> str:
        return self.private_key

    def _endpoint(self, props: Mapping[str, Any]) -> str:
        return endpoint(props)

    def _version(self) -> str:
        return VERSION

    def _device(self, props: Mapping[str, Any]) -> ssh.Device:
        """The device a property bag names, opened with the configured credential."""
        return ssh.Device(
            host=str(props['host']),
            port=int(props['port']),
            username=str(props['username']),
            private_key=self.private_key,
            host_key=str(props['host_key']),
        )

    async def _restamp_only(self, olds: Mapping[str, Any], news: Mapping[str, Any], declared: tuple[str, ...]) -> bool:
        """Whether this update has nothing for the device: only a stamp moved.

        The stamps change without the device changing, so a rotation or a version
        bump must not rewrite every file on the gateway -- the update re-stamps
        the resource and leaves the device alone. The device still gets the last
        word, because drift is the other reason an update is planned with every
        declared input equal, and `diff` cannot say through its result which of
        the two answers it gave. So it is asked again, and this branch reads:
        writing is what it never does.
        """
        if declared_change(olds, news, declared):
            return False
        return not await self._drifted(news)

    @abc.abstractmethod
    async def _drifted(self, props: Mapping[str, Any]) -> bool:
        """Whether the device disagrees with what `props` declares."""


@final
class DeviceFileProvider(DeviceProvider):
    """One desired-state file on the device: write it, then make it take effect."""

    def check(self, _olds: dict[str, Any], news: dict[str, Any]) -> dynamic.CheckResult:
        return self._stamp(news, [*_absolute(news, 'path'), *_octal(news, 'mode')])

    def create(self, props: dict[str, Any]) -> dynamic.CreateResult:
        run_sync(self._apply(props))
        # The checked inputs go back out as the outputs, stamps included, so the
        # stored bag records the session that wrote this file.
        return dynamic.CreateResult(id_=str(props['path']), outs=props)

    def diff(self, _id: str, olds: dict[str, Any], news: dict[str, Any]) -> dynamic.DiffResult:
        replaces = replacements(olds, news, 'path')
        if replaces:
            # Created before deleted: the paths differ, so the new file exists
            # before the old one goes, and no moment passes with neither there.
            return dynamic.DiffResult(changes=True, replaces=replaces, delete_before_replace=False)
        if has_unknowns(news):
            return dynamic.DiffResult(changes=None)
        if declared_change(olds, news, FILE_COMPARED):
            return dynamic.DiffResult(changes=True, replaces=[])
        return dynamic.DiffResult(changes=run_sync(self._drifted(news)), replaces=[])

    def update(self, _id: str, olds: dict[str, Any], news: dict[str, Any]) -> dynamic.UpdateResult:
        if not run_sync(self._restamp_only(olds, news, FILE_DECLARED)):
            run_sync(self._apply(news))
        # The outs replace the stored output bag (rfc-002 §7.5 E9), so what state
        # says about the session that last wrote this file stays true.
        return dynamic.UpdateResult(outs=news)

    def read(self, id_: str, props: dict[str, Any]) -> dynamic.ReadResult:
        return run_sync(self._read(id_, props))

    def delete(self, _id: str, props: dict[str, Any]) -> None:
        run_sync(self._delete(props))

    async def _apply(self, props: Mapping[str, Any]) -> None:
        async with open_transport(self._device(props)) as transport:
            await transport.write(
                str(props['path']),
                str(props['content']).encode(),
                mode=str(props['mode']),
                owner=_owner(props),
            )
            await run_hook(transport, props)

    async def _drifted(self, props: Mapping[str, Any]) -> bool:
        path = str(props['path'])
        async with open_transport(self._device(props)) as transport:
            stat = await transport.stat(path)
            if stat is None:
                return True
            if not ssh.same_mode(stat.mode, str(props['mode'])) or not ssh.same_owner(_owner(props), stat):
                return True
            return await transport.read(path) != str(props['content']).encode()

    async def _read(self, id_: str, props: Mapping[str, Any]) -> dynamic.ReadResult:
        path = str(props['path'])
        async with open_transport(self._device(props)) as transport:
            stat = await transport.stat(path)
            content = None if stat is None else await transport.read(path)
            if stat is None or content is None:
                # Gone from the device is gone: the next up creates it again,
                # which is how a file someone deleted by hand comes back.
                return gone()
            outs = {
                **props,
                'content': content.decode(errors='replace'),
                'mode': stat.mode,
                'owner': _observed_owner(props.get('owner'), stat),
            }
        return dynamic.ReadResult(id_=id_, outs=outs)

    async def _delete(self, props: Mapping[str, Any]) -> None:
        async with open_transport(self._device(props)) as transport:
            await transport.remove(str(props['path']))
            # The hook runs on the way out too: whatever reads this file has to
            # be told it is gone, exactly as it was told it had arrived.
            await run_hook(transport, props)


@final
class DeviceArtifactProvider(DeviceProvider):
    """A digest-pinned root filesystem on the device, tracked by a marker file."""

    def check(self, _olds: dict[str, Any], news: dict[str, Any]) -> dynamic.CheckResult:
        failures = [*_absolute(news, 'root'), *_registry_qualified(news, 'repository')]
        digest = news.get('digest')
        if digest is not None and not is_unknown(digest) and not _is_digest(str(digest)):
            failures.append(dynamic.CheckFailure('digest', f'must be a `sha256:<hex>` manifest digest, got {digest!r}'))
        return self._stamp(news, failures)

    def create(self, props: dict[str, Any]) -> dynamic.CreateResult:
        run_sync(self._land(props))
        return dynamic.CreateResult(id_=str(props['root']), outs=props)

    def diff(self, _id: str, olds: dict[str, Any], news: dict[str, Any]) -> dynamic.DiffResult:
        replaces = replacements(olds, news, 'root')
        if replaces:
            return dynamic.DiffResult(changes=True, replaces=replaces, delete_before_replace=False)
        if has_unknowns(news):
            return dynamic.DiffResult(changes=None)
        if declared_change(olds, news, ARTIFACT_COMPARED):
            return dynamic.DiffResult(changes=True, replaces=[])
        return dynamic.DiffResult(changes=run_sync(self._drifted(news)), replaces=[])

    def update(self, _id: str, olds: dict[str, Any], news: dict[str, Any]) -> dynamic.UpdateResult:
        # A re-stamp pulls nothing either: the tree the device holds is the
        # image the pin names, and neither end has a reason to fetch it again to
        # find that out.
        if not run_sync(self._restamp_only(olds, news, ARTIFACT_DECLARED)):
            run_sync(self._land(news))
        return dynamic.UpdateResult(outs=news)

    def read(self, id_: str, props: dict[str, Any]) -> dynamic.ReadResult:
        return run_sync(self._read(id_, props))

    def delete(self, _id: str, props: dict[str, Any]) -> None:
        run_sync(self._delete(props))

    async def _land(self, props: Mapping[str, Any]) -> None:
        """Pull, unpack, claim, notify -- all of it on the far end of one session.

        Every step is a command the device runs, and a step that fails ends the
        operation with what the device said about it. Nothing before the second
        rename touches the live tree, so a pull that could not reach the
        registry and an unpack that ran out of disk both leave the container
        running exactly what it was running.
        """
        root = str(props['root'])
        digest = str(props['digest'])
        image = reference(str(props['repository']), digest)
        async with open_transport(self._device(props)) as transport:
            pull = await transport.run(pull_script(image, root))
            if not pull.ok:
                raise PullFailed(image, pull.exit_status, pull.stderr)
            unpack = await transport.run(unpack_script(root))
            if not unpack.ok:
                raise UnpackFailed(root, unpack.exit_status, unpack.stderr)
            # Before the hook: the hook reads this.
            await _mark(transport, root, digest)
            try:
                await run_hook(transport, props)
            except BaseException:
                # Withdrawn on any hook that did not succeed: the claim is no
                # longer true. Best-effort -- a session that is gone removes
                # nothing, and the failure worth reporting is the one from here.
                with suppress(Exception):
                    await transport.remove(marker_path(root))
                raise

    async def _drifted(self, props: Mapping[str, Any]) -> bool:
        root = str(props['root'])
        pinned = str(props['digest'])
        async with open_transport(self._device(props)) as transport:
            return not await _tree_holds(transport, root, pinned)

    async def _read(self, id_: str, props: Mapping[str, Any]) -> dynamic.ReadResult:
        root = str(props['root'])
        async with open_transport(self._device(props)) as transport:
            stat = await transport.stat(root)
            marker = None if stat is None else await transport.read(marker_path(root))
            if stat is None or marker is None:
                # A tree with no marker is a tree of unknown provenance, which
                # is the same situation as no tree at all.
                return gone()
            outs = {**props, 'digest': marker.decode(errors='replace').strip()}
        return dynamic.ReadResult(id_=id_, outs=outs)

    async def _delete(self, props: Mapping[str, Any]) -> None:
        root = str(props['root'])
        async with open_transport(self._device(props)) as transport:
            await transport.remove(marker_path(root))
            _ = await transport.run(purge_script(root))
            await run_hook(transport, props)


@final
class DeviceDirectoryProvider(DeviceProvider):
    """One directory the device must have, whose contents belong to somebody else.

    The shape is the file provider's without the content: what is converged is
    existence, mode and ownership, and the device is asked about all three on
    every diff. What is not here is any statement about the contents --
    they are why the directory was declared, and they are never read, written or
    counted except to refuse a delete that would take them with it.

    **What the device holds at the path is part of the comparison**, not merely
    whether something is there: `stat` reports the kind, so a regular file
    somebody left where a directory is declared is drift, and a refresh records
    the resource as gone rather than as converged. The operations refuse it too
    -- `mkdir -p` will not take a path a file occupies, and neither will `rmdir`
    -- but a diff that could not see it would report no change for as long as
    the mode and the owner happened to match, which is exactly the directory
    this resource exists to notice.
    """

    def check(self, _olds: dict[str, Any], news: dict[str, Any]) -> dynamic.CheckResult:
        return self._stamp(news, [*_absolute(news, 'path'), *_octal(news, 'mode')])

    def create(self, props: dict[str, Any]) -> dynamic.CreateResult:
        run_sync(self._make(props))
        return dynamic.CreateResult(id_=str(props['path']), outs=props)

    def diff(self, _id: str, olds: dict[str, Any], news: dict[str, Any]) -> dynamic.DiffResult:
        replaces = replacements(olds, news, 'path')
        if replaces:
            # Created before deleted, as a moved file is: the new directory
            # exists before the old one is taken away, and the old one is taken
            # away only if nothing has filled it in the meantime.
            return dynamic.DiffResult(changes=True, replaces=replaces, delete_before_replace=False)
        if has_unknowns(news):
            return dynamic.DiffResult(changes=None)
        if declared_change(olds, news, DIRECTORY_COMPARED):
            return dynamic.DiffResult(changes=True, replaces=[])
        return dynamic.DiffResult(changes=run_sync(self._drifted(news)), replaces=[])

    def update(self, _id: str, olds: dict[str, Any], news: dict[str, Any]) -> dynamic.UpdateResult:
        if not run_sync(self._restamp_only(olds, news, DIRECTORY_DECLARED)):
            run_sync(self._make(news))
        return dynamic.UpdateResult(outs=news)

    def read(self, id_: str, props: dict[str, Any]) -> dynamic.ReadResult:
        return run_sync(self._read(id_, props))

    def delete(self, _id: str, props: dict[str, Any]) -> None:
        run_sync(self._delete(props))

    async def _make(self, props: Mapping[str, Any]) -> None:
        """Make the directory, then tell whatever was waiting for it.

        One script for the create and the update both, because converging a mode
        and making a directory that is already there are the same three commands.
        """
        path = str(props['path'])
        async with open_transport(self._device(props)) as transport:
            made = await transport.run(make_script(path, str(props['mode']), _owner(props)))
            if not made.ok:
                raise MakeDirectoryFailed(path, made.exit_status, made.stderr)
            await run_hook(transport, props)

    async def _drifted(self, props: Mapping[str, Any]) -> bool:
        async with open_transport(self._device(props)) as transport:
            stat = await transport.stat(str(props['path']))
            if stat is None or not stat.is_directory:
                # Which is the gap this resource closes: a directory removed by
                # hand, taken by a firmware update, or replaced by a file of the
                # same name is work the next preview sees without anyone asking
                # for a refresh.
                return True
            return not ssh.same_mode(stat.mode, str(props['mode'])) or not ssh.same_owner(_owner(props), stat)

    async def _read(self, id_: str, props: Mapping[str, Any]) -> dynamic.ReadResult:
        async with open_transport(self._device(props)) as transport:
            stat = await transport.stat(str(props['path']))
            if stat is None or not stat.is_directory:
                # Gone from the device is gone, as it is for a file, and a path
                # something else occupies is a directory that is not there: the
                # next up makes it again, and says so if it cannot.
                return gone()
            outs = {**props, 'mode': stat.mode, 'owner': _observed_owner(props.get('owner'), stat)}
        return dynamic.ReadResult(id_=id_, outs=outs)

    async def _delete(self, props: Mapping[str, Any]) -> None:
        path = str(props['path'])
        async with open_transport(self._device(props)) as transport:
            removed = await transport.run(remove_script(path))
            if removed.exit_status == ssh.NOT_EMPTY:
                raise DirectoryNotEmpty(path)
            if not removed.ok:
                raise RemoveDirectoryFailed(path, removed.exit_status, removed.stderr)
            # And the hook on the way out, as a file's delete runs it: whatever
            # was told the directory had arrived is told it is gone.
            await run_hook(transport, props)


@final
class DeviceFile(dynamic.Resource, module='device', name='File'):
    """A file the device must have, and what to run once it has it."""

    path: pulumi.Output[str]
    content: pulumi.Output[str]
    mode: pulumi.Output[str]
    owner: pulumi.Output[str | None]
    hook: pulumi.Output[str | None]

    def __init__(
        self,
        name: str,
        *,
        connection: Connection,
        path: pulumi.Input[str],
        content: pulumi.Input[str],
        mode: pulumi.Input[str] = '0644',
        owner: pulumi.Input[str] | None = None,
        hook: pulumi.Input[str] | None = None,
        secret: bool = False,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        """Declare `path` on the device.

        `owner` is `user` or `user:group`, and omitting it leaves ownership to
        whatever the device does by default. `hook` runs after every write and
        after the delete; a non-zero exit fails the operation. `secret=True`
        marks the content a credential, for the files that carry one.
        """
        super().__init__(
            DeviceFileProvider(),
            name,
            {
                **connection.props(),
                'path': path,
                'content': content,
                'mode': mode,
                'owner': owner,
                'hook': hook,
            },
            pulumi.ResourceOptions.merge(
                pulumi.ResourceOptions(additional_secret_outputs=secret_outputs(secret_content=secret)),
                opts,
            ),
        )


@final
class DeviceDirectory(dynamic.Resource, module='device', name='Directory'):
    """A directory the device must have, whose contents are somebody else's."""

    path: pulumi.Output[str]
    mode: pulumi.Output[str]
    owner: pulumi.Output[str | None]
    hook: pulumi.Output[str | None]

    def __init__(
        self,
        name: str,
        *,
        connection: Connection,
        path: pulumi.Input[str],
        mode: pulumi.Input[str] = '0755',
        owner: pulumi.Input[str] | None = None,
        hook: pulumi.Input[str] | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        """Declare that `path` is a directory on the device.

        `owner` is `user` or `user:group`, and omitting it leaves ownership to
        whatever the device does by default. `hook` runs after the directory is
        made or its shape converged, and after the delete; a non-zero exit fails
        the operation.

        **The directory is declared and its contents are not.** That is the point
        of the resource -- it is for a directory something on the device fills at
        runtime -- and it is why the delete removes an empty directory and
        refuses a full one: what is inside was put there by whoever fills it, and
        this program never claimed it.

        `path` is therefore the only replacement. A directory cannot be in two
        places, and one left behind at the old path would be an orphan nothing
        declares -- while everything else, the address dialled included, is a
        value the next apply converges where the directory already is.
        """
        super().__init__(
            DeviceDirectoryProvider(),
            name,
            {
                **connection.props(),
                'path': path,
                'mode': mode,
                'owner': owner,
                'hook': hook,
            },
            # A path, a mode and an owner: nothing here is a secret, and a
            # preview that shows them is a preview a reviewer can read.
            opts,
        )


@final
class DeviceArtifact(dynamic.Resource, module='device', name='Artifact'):
    """A digest-pinned container image, unpacked on the device as a tree."""

    repository: pulumi.Output[str]
    tag: pulumi.Output[str]
    digest: pulumi.Output[str]
    root: pulumi.Output[str]
    hook: pulumi.Output[str | None]

    def __init__(
        self,
        name: str,
        *,
        connection: Connection,
        repository: pulumi.Input[str],
        tag: pulumi.Input[str],
        digest: pulumi.Input[str],
        root: pulumi.Input[str],
        hook: pulumi.Input[str] | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        """Declare that `root` holds the root filesystem `digest` names.

        The bytes never enter state and never cross the runner: at apply time
        the device is handed `repository@digest` and pulls it itself, and it
        keeps a marker beside the tree naming what it holds. `tag` is where the
        digest was found and is declared rather than used -- the pull is by
        digest, because a tag is a name somebody else can move.

        The resource owns the directory whole: it replaces it on a new pin and
        removes it on a delete, along with the staging siblings the push works
        through. `root` is therefore the only replacement -- the tree cannot be
        in two places, and one left at the old path would be an orphan nothing
        declares.

        Ownership and mode are not among the inputs. What a root filesystem's
        files belong to and what they may do is the image's own statement, and
        it is `umoci` on the device that restores it.
        """
        super().__init__(
            DeviceArtifactProvider(),
            name,
            {
                **connection.props(),
                'repository': repository,
                'tag': tag,
                'digest': digest,
                'root': root,
                'hook': hook,
            },
            # No property of an artifact is a secret: a reference, a digest and
            # a path are all things a reviewer should read in a preview.
            opts,
        )


async def _mark(transport: ssh.Transport, location: str, digest: str) -> None:
    """Record which published artifact the tree at `location` came from."""
    await transport.write(marker_path(location), f'{digest}\n'.encode(), mode=MARKER_MODE, owner=None)


async def _tree_holds(transport: ssh.Transport, directory: str, digest: str) -> bool:
    """Whether the device has a tree at `directory` unpacked from `digest`.

    Both halves are asked about, because either can be false on its own: a
    firmware update takes the tree and leaves nothing, and a marker naming
    another digest is a tree from a pin nobody declares any more.
    """
    if await transport.stat(directory) is None:
        return False
    marker = await transport.read(marker_path(directory))
    return marker is not None and marker.decode(errors='replace').strip() == digest


def _owner(props: Mapping[str, Any]) -> str | None:
    owner = props.get('owner')
    return str(owner) if owner else None


def _is_digest(value: str) -> bool:
    """Whether this is a registry digest, in the one spelling a registry uses.

    Lower case and algorithm-qualified, both because that is the form a
    reference carries and because the marker on the device is compared byte for
    byte -- a differently-spelled digest is a pin that never matches.
    """
    algorithm, separator, hex_digest = value.partition(':')
    return (
        algorithm == 'sha256'
        and bool(separator)
        and len(hex_digest) == 64
        and all(character in '0123456789abcdef' for character in hex_digest)
    )
