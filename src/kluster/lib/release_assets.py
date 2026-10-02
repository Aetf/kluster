"""Release assets a pin records by sha256: fetched, and refused unless they are those bytes.

Run by the stack program that applies a release manifest and by `update_crds`,
which renders definitions from one (docs/style/pulumi.md, "Layering", for why
code both run lives in `kluster.lib`). It imports no generated SDK, unlike
`kluster.lib.k8s`: `update_crds` is what regenerates `sdks/crds`, so it has to
start when that SDK does not import.
"""

from __future__ import annotations

import hashlib

import requests

from kluster.lib.versions import ManifestPin

__all__ = ('ManifestDigestMismatch', 'fetch_manifest')


class ManifestDigestMismatch(ValueError):
    """A downloaded release asset is not the bytes its pin records."""


def fetch_manifest(pin: ManifestPin) -> str:
    """The release asset a manifest pin names, refused unless its sha256 is the pin's.

    Both readers read the same bytes and refuse the same substitution: a
    release asset can be uploaded again under its tag, and the pin's sha256 is
    what holds the tag to what was proposed. The whole body is held before it
    is checked, so nothing reads an asset whose digest has not matched. A
    mismatch names both digests, because the one thing the caller has to
    decide is whether the pin is stale or the download is not the artifact it
    claims to be.
    """
    response = requests.get(pin.url, timeout=60)
    _ = response.raise_for_status()
    digest = hashlib.sha256(response.content).hexdigest()
    if digest != pin.sha256:
        raise ManifestDigestMismatch(f'{pin.key}: {pin.url} has sha256 {digest}, and the pin records {pin.sha256}')
    return response.content.decode()
