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

#: The prefix every type token of the device_files provider carries.
DEVICE_TYPE_PREFIX = 'pulumi-python:dynamic/device:'

#: Type token to the input that is the resource's id, and its place on the device.
PLACES: Mapping[str, str] = {
    f'{DEVICE_TYPE_PREFIX}File': 'path',
    f'{DEVICE_TYPE_PREFIX}Directory': 'path',
    f'{DEVICE_TYPE_PREFIX}Artifact': 'root',
}
