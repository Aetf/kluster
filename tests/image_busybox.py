"""The busybox a pinned image carries, read out of the image by its digest and laid out to run on the host.

The monitoring stack's backup and restore scripts run in the curl and
`vmrestore` images, whose `/bin/sh` and every tool the scripts call are the
busybox each carries. The suite runs each script under the bytes of the image
its container runs: `extracted` reads `/bin/busybox` out of the image a pin
names, so a move of the pin moves the suite's shell with it.

The binary is dynamically linked. Its interpreter, the ELF program header's
`PT_INTERP`, is the one other file it needs: Alpine builds busybox against
musl, whose dynamic loader is also its C library, the `libc.musl-*` the binary
names as its one `NEEDED` entry. The host has no file at that path, so the
loader is read out beside the binary, and `applets` lays out each applet,
`busybox` among them, as a script whose interpreter line runs the image's
loader on the image's binary. The kernel hands the script's path on as the
next argument, and busybox, run as `busybox <path> <args>`, runs the applet
the path's last component names, the form busybox keeps for `#!/bin/busybox`
wrappers. A busybox with no interpreter takes the same line with the loader
left out. Both files are the image's bytes, unchanged. What a process sees of
itself differs from the pod in two files no script reads: `/proc/<pid>/exe`
is the loader, and `/proc/<pid>/cmdline` carries the loader's and the
binary's paths ahead of the applet's.

Each file is read by a container of the image that writes it to its standard
output and ends, started with `--rm`, a `--timeout` and the module's label as
every container a case starts is, and named for the teardown that removes it,
armed before the `podman run` (framework/testing.md §1.2).

The reading runs where podman is on `PATH`. Anywhere else a case that needs
it is skipped, naming why, except on CI's runner, which has podman and where
the case must run: there it fails. Pulling the pinned images is a network
call a unit test otherwise does not make (framework/testing.md §7): the
bytes the scripts run under are the images' only by being read out of them.
The files are cached under pytest's cache directory keyed by the image's
digest, so the pull and the read happen once for a pin, and a worker that
finds another reading waits for it. A run with that cache turned off, as
framework/testing.md §6's mutation runs are, reads into a temporary
directory of its own instead.
"""

from __future__ import annotations

import fcntl
import hashlib
import os
import shutil
import struct
import uuid
from pathlib import Path, PurePosixPath

import process_sessions
import pytest

from kluster.lib.versions import ImagePin, ProjectFile, Versions
from kluster.scripts.update_crds import sources

ROOT = Path(__file__).resolve().parents[1]

#: Where an Alpine image keeps its busybox, which `/bin/sh` and every applet link to.
BINARY = '/bin/busybox'

#: Docker Hub's prefix on a pinned repository, and Google's mirror of it.
DOCKER_HUB = 'docker.io/'
DOCKER_HUB_MIRROR = 'mirror.gcr.io/'

#: The directory of `applets`' layout that holds the image's own files, apart
#: from the applets on `PATH`.
IMAGE = 'image'

#: The label a container started here carries, and so the census of what a run
#: has left: `podman ps --all --filter label=kluster-test=image_busybox`.
LABEL = f'kluster-test={Path(__file__).stem}'

#: The bound on a pull, which reaches a registry: a stop-loss, below the case
#: bound, which nothing asserts on.
PULL_TIMEOUT = 45
#: The bound on each other podman command, every one local.
COMMAND_TIMEOUT = 30
#: How long a container started here may run: the run's outer bound
#: (framework/testing.md §1.2).
CONTAINER_TIMEOUT = 1200

#: The longest interpreter line the kernel reads, `#!` and the newline
#: included (`BINPRM_BUF_SIZE`); a longer one fails the `exec`.
INTERPRETER_LINE_LIMIT = 256

#: The ELF program header type naming the interpreter.
_PT_INTERP = 3


def pinned(name: str) -> ImagePin:
    """The image `versions:image-<name>` pins in `Pulumi.yaml`, the reference the deployment pulls."""
    return Versions(ProjectFile(sources.read_config(ROOT / 'Pulumi.yaml'))).image[name]


def require_podman() -> None:
    """Skips the calling case where podman is not on `PATH`, and fails it there on CI's runner."""
    if shutil.which('podman') is not None:
        return
    why = 'podman is not on PATH, and the busybox the scripts run under is read out of a pinned image with it'
    # GitHub sets `GITHUB_ACTIONS` on every job's runner, which has podman.
    if os.environ.get('GITHUB_ACTIONS') == 'true':
        pytest.fail(f'{why}; CI runs these cases')
    pytest.skip(why)


def interpreter(binary: bytes) -> str | None:
    """The path a 64-bit little-endian ELF executable names as its interpreter, `None` where it names none."""
    if binary[:6] != b'\x7fELF\x02\x01':
        raise ValueError('not a 64-bit little-endian ELF file')
    (table,) = struct.unpack_from('<Q', binary, 0x20)
    size, count = struct.unpack_from('<HH', binary, 0x36)
    for header in range(table, table + size * count, size):
        (kind,) = struct.unpack_from('<I', binary, header)
        if kind == _PT_INTERP:
            offset, _, _, length = struct.unpack_from('<QQQQ', binary, header + 8)
            return binary[offset : offset + length].rstrip(b'\0').decode()
    return None


def _podman(*args: str, timeout: float = COMMAND_TIMEOUT) -> bytes:
    done = process_sessions.run(['podman', *args], timeout=timeout)
    if done.returncode != 0:
        raise AssertionError(f'podman {" ".join(args[:2])} failed: {done.stderr.decode().strip()}')
    return done.stdout


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


def _pulled(image: ImagePin) -> str:
    reference = source(image)
    if process_sessions.run(['podman', 'image', 'exists', reference], timeout=COMMAND_TIMEOUT).returncode != 0:
        _ = _podman('pull', '--quiet', reference, timeout=PULL_TIMEOUT)
    return reference


def in_image(image: ImagePin, *argv: str) -> bytes:
    """What `argv`, an applet of the image's own busybox, writes to its standard output in a container of `image`."""
    require_podman()
    name = f'kluster-test-busybox-{uuid.uuid4().hex[:12]}'
    with process_sessions.removing(name, timeout=COMMAND_TIMEOUT):
        return _podman(
            'run',
            '--rm',
            '--timeout',
            str(CONTAINER_TIMEOUT),
            '--label',
            LABEL,
            '--name',
            name,
            '--network',
            'none',
            '--entrypoint',
            BINARY,
            _pulled(image),
            *argv,
        )


def _read_out(image: ImagePin, into: Path) -> None:
    """`BINARY` and its interpreter, read out of `image` into `into` under their own names."""
    binary = into / PurePosixPath(BINARY).name
    _ = binary.write_bytes(in_image(image, 'cat', BINARY))
    loader = interpreter(binary.read_bytes())
    if loader is not None:
        _ = (into / PurePosixPath(loader).name).write_bytes(in_image(image, 'cat', loader))


def cache_directory(config: pytest.Config, tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Where `extracted` keeps the files: pytest's cache directory, or the run's temporary one where that is turned off."""
    if config.pluginmanager.has_plugin('cacheprovider'):
        return config.cache.mkdir('image-busybox')
    return tmp_path_factory.mktemp('image-busybox')


def extracted(image: ImagePin, cache: Path) -> Path:
    """The directory under `cache` holding `image`'s busybox and its interpreter, read on the first call for the digest.

    A partial read never takes the digest's name: the files are written into
    a directory of the caller's own and renamed into place whole.
    """
    require_podman()
    digest = image.digest.removeprefix('sha256:')
    done = cache / digest
    with (cache / f'{digest}.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not done.is_dir():
            partial = cache / f'{digest}.{uuid.uuid4().hex[:12]}'
            partial.mkdir()
            try:
                _read_out(image, partial)
                partial.rename(done)
            finally:
                shutil.rmtree(partial, ignore_errors=True)
    return done


def applets(files: Path, into: Path) -> Path:
    """`into`, holding a script for each applet the busybox in `files` lists, and the busybox and its interpreter under `IMAGE`."""
    image = into / IMAGE
    image.mkdir()
    binary = image / PurePosixPath(BINARY).name
    launch = [binary]
    loader = interpreter((files / binary.name).read_bytes())
    if loader is not None:
        launch.insert(0, image / PurePosixPath(loader).name)
    for path in launch:
        _ = shutil.copyfile(files / path.name, path)
        path.chmod(0o755)
    line = f'#!{" ".join(map(str, launch))}\n'
    assert not any(any(it.isspace() for it in str(path)) for path in launch), f'the kernel splits {line!r} at a space'
    assert len(line) <= INTERPRETER_LINE_LIMIT, f'the kernel refuses the {len(line)}-byte interpreter line {line!r}'
    listed = process_sessions.run([*map(str, launch), '--list'], text=True, timeout=COMMAND_TIMEOUT, check=True)
    for applet in {*listed.stdout.split(), binary.name}:
        script = into / applet
        _ = script.write_text(line)
        script.chmod(0o755)
    return into


def launched(applet: Path) -> list[Path]:
    """The files the kernel runs for `applet`: those its interpreter line names, or the file it resolves to."""
    with applet.open('rb') as file:
        first = file.readline()
    if not first.startswith(b'#!'):
        return [applet.resolve()]
    return [Path(it) for it in first[2:].decode().split()]


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def sums_in(image: ImagePin, *paths: str) -> dict[str, str]:
    """The sha256 of each of `paths` inside `image`, computed by the image's own busybox."""
    listed = in_image(image, 'sha256sum', *paths).decode()
    return {path: digest for digest, path in (line.split() for line in listed.splitlines())}
