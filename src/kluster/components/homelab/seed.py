"""The worker's seed image, and the file in the slot it is uploaded from.

Talos' `nocloud` platform finds its machine configuration on a filesystem
labelled `cidata`, on any disk the block discovery sees, and reads two files
from it: `user-data`, the machine configuration, and `meta-data`, the
platform's metadata. The seed is that filesystem, built here: an ISO 9660
image with the volume identifier `cidata` and both names in its Rock Ridge and
Joliet trees, so a reader that takes either extension finds them as named.

The image is built by the program rather than by the libvirt provider's
`CloudInitDisk`, whose builder runs a `mkisofs` on the machine running the
stack. No registry `mise.toml` installs from carries a `mkisofs`, a
`genisoimage` or an `xorriso`, so that program could not be pinned, and a
program the provider runs is pinned like every other command-line tool
(framework/pulumi.md §3.2). pycdlib is a locked Python dependency instead.

**It carries no `network-config`.** Talos reads `meta-data` and
`network-config` at every boot, for the platform's network configuration, so
both have to stay as they were at the first boot. An absent `network-config`
is read as absent and the node's cached configuration stands, while an empty
one fails to decode; the node's addressing is in its machine configuration.

**The file is named by what it carries.** Its name is the digest of
`user-data` and `meta-data`, not of the image, whose bytes carry the time the
image was built: one rendering names one file on every run, and a new
rendering names a new one. It is written into the libvirt slot, `0600`
inside the `0700` boundary, because `user-data` holds the cluster's secrets,
and the files an older rendering wrote there are removed.
"""

from __future__ import annotations

import hashlib
import io
from pathlib import Path

import pycdlib

from kluster.lib import workstation

__all__ = ('LABEL', 'META_DATA', 'USER_DATA', 'image', 'write')

#: The volume identifier Talos' `nocloud` platform finds the seed by. It
#: matches `cidata` and `CIDATA` alike, and this is the spelling cloud-init's
#: own tools write.
LABEL = 'cidata'

#: The two files the platform reads, by the names it reads them under.
USER_DATA = 'user-data'
META_DATA = 'meta-data'

#: What a seed file in the slot is called: the prefix and suffix around the
#: digest, which is also what marks a file there as an older seed to remove.
PREFIX = 'seed-'
SUFFIX = '.iso'

#: The ISO 9660 names beside the extensions' own. Interchange level 1 allows
#: eight upper-case characters and no hyphen, and no reader this seed is for
#: looks at them.
_ISO_NAMES = {USER_DATA: '/USERDATA.;1', META_DATA: '/METADATA.;1'}


def image(*, user_data: str, meta_data: str) -> bytes:
    """The seed as an ISO 9660 image: `user-data` and `meta-data` under the label `cidata`."""
    iso = pycdlib.PyCdlib()
    iso.new(vol_ident=LABEL, joliet=3, rock_ridge='1.09')
    try:
        for name, content in ((USER_DATA, user_data), (META_DATA, meta_data)):
            data = content.encode()
            iso.add_fp(io.BytesIO(data), len(data), _ISO_NAMES[name], rr_name=name, joliet_path=f'/{name}')
        built = io.BytesIO()
        iso.write_fp(built)
    finally:
        iso.close()
    return built.getvalue()


def write(directory: Path, *, user_data: str, meta_data: str) -> Path:
    """Write the seed into `directory`, named by what it carries, and remove every older seed beside it.

    The write is `workstation.write_bytes`'s: `0600` from creation, replacing
    a file rather than writing into it, in a directory held to `0700`.
    """
    digest = hashlib.sha256()
    for content in (user_data, meta_data):
        encoded = content.encode()
        # Each half is length-prefixed, so no split of one string between the
        # two names the digest of another.
        digest.update(len(encoded).to_bytes(8, 'big'))
        digest.update(encoded)
    path = directory / f'{PREFIX}{digest.hexdigest()}{SUFFIX}'
    _ = workstation.write_bytes(path, image(user_data=user_data, meta_data=meta_data))
    for stale in directory.glob(f'{PREFIX}*{SUFFIX}'):
        if stale != path:
            stale.unlink()
    return path
