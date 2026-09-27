"""Which input of each device resource type is the place it occupies on the device.

A resource of the device_files provider has as its id the one place on the
device it declares -- `DeviceFileProvider.create` answers with `path`,
`DeviceDirectoryProvider.create` with `path`, `DeviceArtifactProvider.create`
with `root` -- and that place is what two resources must not share: Pulumi keys
on URN, so two registrations at one place both `create`, the second write
overwrites the first, the next refresh reports the first as drifted, and a
delete of either removes what the other still declares and runs its hook.
No logical name keeps that from happening (style/pulumi.md), so the program is
held to it as a census over the inputs, `Recorder.places_claimed_more_than_once`
read under this map.

One module, imported by every suite that asserts the census, so that a
resource type added to the provider is added here once. The physical suite
holds the map complete: every device type the program registers is a key here.
"""

from __future__ import annotations

from collections.abc import Mapping

from pulumi import dynamic

from kluster.providers.device_files.provider import DeviceArtifact, DeviceDirectory, DeviceFile


def stated_type(cls: type[dynamic.Resource]) -> str:
    """The type token `cls` registers under: the SDK's `pulumi-python:dynamic`, then the `module`/`name` it states."""
    # The SDK keeps the stated half on this attribute and nowhere public.
    return f'pulumi-python:{cls._resource_type_name}'  # pyright: ignore[reportPrivateUsage]


DEVICE_FILE = stated_type(DeviceFile)
DEVICE_DIRECTORY = stated_type(DeviceDirectory)
DEVICE_ARTIFACT = stated_type(DeviceArtifact)

#: The prefix every type token of the device_files provider carries.
DEVICE_TYPE_PREFIX = DEVICE_FILE.rpartition(':')[0] + ':'

#: Type token to the input that is the resource's id, and its place on the device.
PLACES: Mapping[str, str] = {
    DEVICE_FILE: 'path',
    DEVICE_DIRECTORY: 'path',
    DEVICE_ARTIFACT: 'root',
}
