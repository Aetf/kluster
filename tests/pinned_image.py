"""A pinned image, pulled by its digest for a case that runs it or reads a file out of it.

A pin is the one `Pulumi.yaml` holds (`pinned`) where the deployment pulls
the image, so a move of the pin moves what the suite runs with it. An image
nothing deploys is pinned in the module whose cases run it, beside what keeps
the pin on the fleet's: today the control plane's, in
`tests/test_apps_database.py`, held to the Talos release's listing. The image is pulled from Google's mirror where its
repository is Docker Hub's, by digest (`source`, `pulled`), and only where it
is not already local.

**The cases run where podman is on `PATH`.** Anywhere else such a case is
skipped, naming why, except on CI's runner, which has podman and where the
case must run: there it fails (`require_podman`). Pulling a pinned image is a
network call a unit test otherwise does not make (framework/testing.md §7):
what the image carries is the deployment's only by being read out of it.

A container a case starts from it carries `--rm`, a `--timeout` at
`CONTAINER_TIMEOUT` and its module's label, and is named for the teardown
that removes it, armed before the command that creates it
(framework/testing.md §1).
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

import process_sessions
import pytest

from kluster.lib.versions import ImagePin, ProjectFile, Versions
from kluster.scripts.update_crds import sources

ROOT = Path(__file__).resolve().parents[1]

#: Docker Hub's prefix on a pinned repository, and Google's mirror of it.
DOCKER_HUB = 'docker.io/'
DOCKER_HUB_MIRROR = 'mirror.gcr.io/'

#: The bound on a pull, which reaches a registry: a stop-loss, below the case
#: bound, which nothing asserts on.
PULL_TIMEOUT = 45
#: The bound on each other podman command, every one local.
COMMAND_TIMEOUT = 30
#: How long a container a case starts may run: the run's outer bound
#: (framework/testing.md §1.2).
CONTAINER_TIMEOUT = 1200


def pinned(name: str) -> ImagePin:
    """The image `versions:image-<name>` pins in `Pulumi.yaml`, the reference the deployment pulls."""
    return Versions(ProjectFile(sources.read_config(ROOT / 'Pulumi.yaml'))).image[name]


def require_podman(why: str) -> None:
    """Skips the calling case where podman is not on `PATH`, saying `why` it needs podman, and fails it there on CI's runner."""
    if shutil.which('podman') is not None:
        return
    reason = f'podman is not on PATH, and {why}'
    # GitHub sets `GITHUB_ACTIONS` on every job's runner, which has podman.
    if os.environ.get('GITHUB_ACTIONS') == 'true':
        pytest.fail(f'{reason}; CI runs these cases')
    pytest.skip(reason)


def source(image: ImagePin) -> str:
    """The reference `image` is pulled by here: its digest, from Google's mirror where the repository is Docker Hub's.

    Docker Hub limits anonymous pulls per address, and CI's runner shares its
    address, so a pull from Docker Hub itself fails at random there
    (framework/ci.md §3 says the same of the Postgres image). By the digest,
    a registry serves the pinned image or nothing.
    """
    repository = image.repository
    if repository.startswith(DOCKER_HUB):
        repository = DOCKER_HUB_MIRROR + repository.removeprefix(DOCKER_HUB)
    return f'{repository}@{image.digest}'


def pulled(image: ImagePin) -> str:
    """`source(image)`, pulled first where it is not local."""
    reference = source(image)
    if process_sessions.run(['podman', 'image', 'exists', reference], timeout=COMMAND_TIMEOUT).returncode != 0:
        _ = process_sessions.run(['podman', 'pull', '--quiet', reference], timeout=PULL_TIMEOUT, check=True)
    return reference
