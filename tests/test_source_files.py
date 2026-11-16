"""Files of a source tree, fetched at the pin's commit and refused unless each is the bytes the pin records.

The network is replaced at the one seam the fetch reaches it through, so what
is under test is which URL each file is asked for and what is done with the
answer (docs/framework/pulumi.md §3.2).
"""

from __future__ import annotations

import hashlib

import pytest

from kluster.lib import source_files
from kluster.lib.source_files import SourceDigestMismatch, fetch_source_files
from kluster.lib.versions import SourcePin

#: Two files, small enough to read; the pin below records their sha256s.
FILES = {
    'client/config/crd/classes.yaml': b'kind: CustomResourceDefinition\nmetadata:\n  name: classes.example.com\n',
    'client/config/crd/snapshots.yaml': b'kind: CustomResourceDefinition\nmetadata:\n  name: snapshots.example.com\n',
}

PIN = SourcePin(
    name='external-snapshotter',
    repository='kubernetes-csi/external-snapshotter',
    tag='v8.7.0',
    commit='0123456789abcdef0123456789abcdef01234567',
    files={path: hashlib.sha256(content).hexdigest() for path, content in FILES.items()},
)


class FakeFile:
    """A downloaded file holding the bytes it was given."""

    def __init__(self, content: bytes) -> None:
        self.content: bytes = content

    def raise_for_status(self) -> None:
        return None


def serve(monkeypatch: pytest.MonkeyPatch, files: dict[str, bytes]) -> list[str]:
    """Answer each URL with the file whose path it ends in, and hand back the URLs asked for."""
    requested: list[str] = []

    def get(url: str, **_: object) -> FakeFile:
        requested.append(url)
        (content,) = [content for path, content in files.items() if url.endswith(f'/{path}')]
        return FakeFile(content)

    monkeypatch.setattr(source_files.requests, 'get', get)
    return requested


def test_each_file_is_fetched_at_the_pins_commit_and_returned_in_the_pins_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """By the commit, not the tag: a tag can be moved, and the commit is what the digests were taken at."""
    requested = serve(monkeypatch, FILES)

    assert fetch_source_files(PIN) == [content.decode() for content in FILES.values()]
    assert requested == [
        'https://raw.githubusercontent.com/kubernetes-csi/external-snapshotter/'
        '0123456789abcdef0123456789abcdef01234567/client/config/crd/classes.yaml',
        'https://raw.githubusercontent.com/kubernetes-csi/external-snapshotter/'
        '0123456789abcdef0123456789abcdef01234567/client/config/crd/snapshots.yaml',
    ]


def test_a_file_whose_digest_differs_refuses_the_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    """One byte off in the last file refuses the pin, naming its key, the file's URL and both digests.

    The last, behind a file that matches, so a check of the first file alone
    would pass it. Both digests, since what the caller has to decide is which
    of the two is wrong.
    """
    altered = FILES['client/config/crd/snapshots.yaml'].replace(b'snapshots', b'snapshotz')
    _ = serve(monkeypatch, FILES | {'client/config/crd/snapshots.yaml': altered})

    with pytest.raises(SourceDigestMismatch, match='versions:source-external-snapshotter') as refused:
        _ = fetch_source_files(PIN)
    message = str(refused.value)
    assert '/client/config/crd/snapshots.yaml' in message
    assert hashlib.sha256(altered).hexdigest() in message
    assert PIN.files['client/config/crd/snapshots.yaml'] in message
