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

The image is the pin's, pulled by its digest, and the reading runs where
podman is on `PATH`, both as `pinned_image` has them: the bytes the scripts
run under are the images' only by being read out of them.

**A file of an image with no busybox** is read out by `copied`, from a
container of the image created and never started: the history store's own
binary, which `tests/test_perf.py` runs as the store it imports into, so
that store is the one the pin deploys.

The files are cached under pytest's cache directory (`cached`), keyed by the
image's digest, and by the path too for `copied`, so the pull and the read
happen once for a pin, and a worker that finds another reading waits for
it. A run with that cache turned off, as
framework/testing.md §6's mutation runs are, reads into a temporary
directory of its own instead.
"""

from __future__ import annotations

import fcntl
import hashlib
import shutil
import struct
import uuid
from collections.abc import Callable
from pathlib import Path, PurePosixPath

import process_sessions
import pytest
from pinned_image import COMMAND_TIMEOUT, CONTAINER_TIMEOUT, pulled, require_podman

# Reached through this module by the suites that read a pinned image's busybox.
from pinned_image import pinned as pinned
from pinned_image import source as source

from kluster.lib.versions import ImagePin

#: Where an Alpine image keeps its busybox, which `/bin/sh` and every applet link to.
BINARY = '/bin/busybox'

#: The directory of `applets`' layout that holds the image's own files, apart
#: from the applets on `PATH`.
IMAGE = 'image'

#: The label a container started here carries, and so the census of what a run
#: has left: `podman ps --all --filter label=kluster-test=image_busybox`.
LABEL = f'kluster-test={Path(__file__).stem}'

#: Why a case that reads an image's files needs podman.
WHY = 'the files a case runs are read out of a pinned image with it'

#: The longest interpreter line the kernel reads, `#!` and the newline
#: included (`BINPRM_BUF_SIZE`); a longer one fails the `exec`.
INTERPRETER_LINE_LIMIT = 256

#: The ELF program header type naming the interpreter.
_PT_INTERP = 3


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


def in_image(image: ImagePin, *argv: str) -> bytes:
    """What `argv`, an applet of the image's own busybox, writes to its standard output in a container of `image`."""
    require_podman(WHY)
    name = f'kluster-test-busybox-{uuid.uuid4().hex[:12]}'
    with process_sessions.removing(name, timeout=COMMAND_TIMEOUT):
        return _podman(
            'run',
            '--rm',
            '--timeout',
            str(CONTAINER_TIMEOUT),
            '--label',
            LABEL,
            process_sessions.LOG_TO_FILE,
            '--name',
            name,
            '--network',
            'none',
            '--entrypoint',
            BINARY,
            pulled(image),
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


def cached(cache: Path, key: str, fill: Callable[[Path], None]) -> Path:
    """The directory `key` under `cache`, which `fill` fills on the first call for the key.

    A worker that finds another filling it waits for it. A partial fill never
    takes the key's name: `fill` writes into a directory of the caller's own,
    which is renamed into place whole.
    """
    done = cache / key
    with (cache / f'{key}.lock').open('w') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if not done.is_dir():
            partial = cache / f'{key}.{uuid.uuid4().hex[:12]}'
            partial.mkdir()
            try:
                fill(partial)
                partial.rename(done)
            finally:
                shutil.rmtree(partial, ignore_errors=True)
    return done


def extracted(image: ImagePin, cache: Path) -> Path:
    """The directory under `cache` holding `image`'s busybox and its interpreter, read on the first call for the digest."""
    require_podman(WHY)
    return cached(cache, image.digest.removeprefix('sha256:'), lambda into: _read_out(image, into))


def copied(image: ImagePin, path: str, cache: Path) -> Path:
    """The file `path` of `image`, read out under `cache` on the first call for the digest and the path.

    For an image that carries no busybox to read a file out with: a container
    of the image is created, never started, under the teardown that removes
    it, and the file copied out of it. Its key is the digest and the path, so
    it never shares a directory with `extracted`'s, or with another path's.
    """
    require_podman(WHY)
    key = f'{image.digest.removeprefix("sha256:")}-{hashlib.sha256(path.encode()).hexdigest()[:12]}'

    def fill(into: Path) -> None:
        name = f'kluster-test-copied-{uuid.uuid4().hex[:12]}'
        with process_sessions.removing(name, timeout=COMMAND_TIMEOUT):
            _ = _podman('create', '--rm', '--label', LABEL, process_sessions.LOG_TO_FILE, '--name', name, pulled(image))
            _ = _podman('cp', f'{name}:{path}', str(into))

    return cached(cache, key, fill) / PurePosixPath(path).name


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
