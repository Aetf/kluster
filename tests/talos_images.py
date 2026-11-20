"""The images the pinned Talos release runs by default, as `talosctl images default` lists them.

`talosctl` reads the listing off the release's own constants, with no
network, so it names the control plane the cluster runs: the Kubernetes
release among them, as the tag of the `kube-apiserver` image. The binary is
the one `mise.toml` pins, found on `PATH` (`pinned_tools.located`).
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import pinned_tools
import process_sessions

#: The bound one `talosctl images default` runs under; it reads the release's
#: own constants and takes well under a second.
TIMEOUT = 12

#: One line of the listing: a repository and a tag, with no digest.
LINE = re.compile(r'^(?P<repository>[^\s@]+):(?P<tag>[^\s:@/]+)$')


@dataclass(frozen=True)
class Image:
    """One line of the listing: an image's repository and tag."""

    repository: str
    tag: str


def default_images() -> dict[str, Image]:
    """Every image the pinned Talos release runs by default, by the last segment of its repository."""
    talosctl = pinned_tools.located('talosctl')
    listed = process_sessions.run([talosctl, 'images', 'default'], text=True, timeout=TIMEOUT, check=True).stdout
    return images_listed(listed)


def images_listed(listed: str) -> dict[str, Image]:
    """The images a listing names, by the last segment of each repository; a line of another shape fails naming it."""
    images: dict[str, Image] = {}
    for line in listed.splitlines():
        if not line.strip():
            continue
        found = LINE.fullmatch(line.strip())
        assert found is not None, f'`talosctl images default` lists {line!r}, which is no repository and tag'
        key = found['repository'].rsplit('/', 1)[-1]
        assert key not in images, f'`talosctl images default` lists two images named {key!r}: {line!r}'
        images[key] = Image(found['repository'], found['tag'])
    return images
