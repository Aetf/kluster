"""Files of a source tree a pin records by commit and sha256: fetched, and refused unless they are those bytes.

The counterpart of `kluster.lib.release_assets` for an upstream that publishes
what is installed only in its source tree. Run by the stack program that
installs the files; `update_crds` reads none (docs/framework/pulumi.md §4).
"""

from __future__ import annotations

import hashlib

import requests

from kluster.lib.versions import SourcePin

__all__ = ('SourceDigestMismatch', 'fetch_source_files')


class SourceDigestMismatch(ValueError):
    """A downloaded file is not the bytes its pin records."""


def fetch_source_files(pin: SourcePin) -> list[str]:
    """Every file a source pin names, at its commit, in the pin's order, refused unless each sha256 is the pin's.

    Fetched by commit rather than by tag, since a tag can be moved and a commit
    cannot. The sha256 is checked all the same, as a manifest pin's is: the
    commit names the bytes only as far as whatever resolves the URL honors it,
    and the digest is checked here, on the bytes that arrived. Every file is
    held and checked before any is returned, so nothing reads one file of a
    pin whose other file has not matched. A mismatch names the URL and both
    digests, because the one thing the caller has to decide is whether the pin
    is stale or the download is not the file it claims to be. A bump renovate
    proposes is the stale case: it moves the tag and the commit, and the
    digests are a person's to record (docs/framework/pulumi.md §3.2).
    """
    texts: list[str] = []
    for path, expected in pin.files.items():
        url = pin.url(path)
        response = requests.get(url, timeout=60)
        _ = response.raise_for_status()
        digest = hashlib.sha256(response.content).hexdigest()
        if digest != expected:
            raise SourceDigestMismatch(f'{pin.key}: {url} has sha256 {digest}, and the pin records {expected}')
        texts.append(response.content.decode())
    return texts
