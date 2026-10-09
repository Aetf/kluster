"""The appliance's pins, and the pinned SSH login to it.

What is left of the appliance's script once the `state-backend` stack declares
the box (rfc-006 §4.5): neither creates nor changes anything the stack
declares, and neither needs an OCI credential.

-   `verify_pins`, behind `state-backend pins`: the pinned artifacts checked
    against their digests, which CI runs on every pull request.
-   `ssh`, behind `state-backend ssh`: a login for diagnosis, held to the host
    key whose public half is committed beside the Butane template
    (`kluster.lib.state_backend.committed.HOST_KEY`), at the address the
    repository records (`settings.ADDRESS`).
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import urllib.error
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import NoReturn, cast

from kluster.lib.state_backend import committed, settings

from ..credentials import workstation
from . import config

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class FcosArtifact:
    """The compressed disk image one FCOS release publishes for this platform."""

    release: str
    url: str
    sha256: str


#: Where the stream metadata keeps this platform's image, key by key. Named
#: because the refusal below quotes it: a stream that stops publishing for
#: Oracle Cloud has to say which step of the descent failed.
_ARTIFACT_PATH = ('architectures', 'x86_64', 'artifacts', 'oraclecloud')


def fcos_artifact() -> FcosArtifact:
    """The pinned stream's qcow2, out of the stream metadata Fedora publishes."""
    log.info('fetching the FCOS %s stream metadata from %s', settings.FCOS_STREAM, settings.FCOS_STREAM_URL)
    with urllib.request.urlopen(settings.FCOS_STREAM_URL, timeout=60) as response:
        stream: object = json.load(response)
    what = f'the FCOS {settings.FCOS_STREAM} stream metadata'
    platform = _descend(stream, _ARTIFACT_PATH, what=what)
    disk = _descend(platform, ('formats', 'qcow2.xz', 'disk'), what=what)
    return FcosArtifact(
        release=_string(platform, 'release', what=what),
        url=_string(disk, 'location', what=what),
        sha256=_string(disk, 'sha256', what=what),
    )


def _descend(document: object, path: Sequence[str], *, what: str) -> object:
    """`document` at `path`, or a refusal naming the key that was not there.

    The boundary for a document this program did not write: what it holds is
    another project's decision, so a key it stops publishing is reported as
    the missing key rather than as a `KeyError` several levels into one
    expression.
    """
    found = document
    for step in path:
        if not isinstance(found, dict) or step not in found:
            raise RuntimeError(f'{what} has no {".".join(path)}: nothing under {step}')
        found = cast('dict[str, object]', found)[step]
    return found


def _string(document: object, key: str, *, what: str) -> str:
    """One non-empty string field of such a document, refused by name if absent."""
    value = cast('dict[str, object]', document).get(key) if isinstance(document, dict) else None
    if not isinstance(value, str) or not value:
        raise RuntimeError(f'{what} has no {key}, and holds {document!r}')
    return value


def pin_options(known_hosts: Path) -> list[str]:
    """The `ssh` options that hold a connection to one host key and to nothing else.

    A named list rather than an argument vector built inline, so that a test
    can drive a real OpenSSH client with the options the tool actually execs
    with: a second copy written in a test would only ever prove that the copy
    works.

    Each one is load-bearing:

    -   `-F /dev/null` -- **no client configuration participates in this
        connection at all.** The cut is stated that way rather than as a list
        of directives to fear, because suppressing the two known-hosts files
        alone leaves the rest of `ssh_config` in force and two of its
        directives defeat the pin outright. `ControlMaster`/`ControlPath`:
        one bare `ssh core@<address>` outside the tool -- the mistake the
        runbook already anticipates -- leaves a multiplexing master, and a
        later pinned exec that sees the same configuration attaches to that
        socket and runs *with no host-key verification performed at all*.
        `KnownHostsCommand`: a command the configuration names supplies
        trusted keys, and its answer is accepted alongside the pinned file.
        `-F` suppresses the system-wide configuration as well as the user's,
        which is what makes this a cut rather than an enumeration: every
        directive not on this command line is back at its default. That is
        the cost too: conveniences an operator keeps in `ssh_config` --
        `IdentityFile`, `ProxyJump`, `ServerAliveInterval` -- do not apply
        here either, so anything this connection needs is named on the
        command line or it does not happen.
    -   `UserKnownHostsFile` and `GlobalKnownHostsFile` -- the pin is the only
        answer this connection accepts, so neither the operator's file nor the
        machine's can supply a competing entry, and the tool writes neither.
    -   `StrictHostKeyChecking=yes` -- no trust-on-first-use and no prompt: an
        unknown or wrong key ends the connection instead of being written down.
    -   `HostKeyAlgorithms` -- the box also holds the host key types
        `sshd-keygen` generated for itself, and naming the pinned algorithm is
        what stops a wrong answer from steering the client onto one of those.

    `CheckHostIP` adds nothing: the connection dials the reserved address
    literally, and the entry is keyed by it.
    """
    return [
        '-F',
        '/dev/null',
        '-o',
        f'UserKnownHostsFile={known_hosts}',
        '-o',
        'GlobalKnownHostsFile=/dev/null',
        '-o',
        'StrictHostKeyChecking=yes',
        '-o',
        'HostKeyAlgorithms=ssh-ed25519',
    ]


def ssh(command: Sequence[str]) -> NoReturn:
    """Log in to the appliance, or run one command on it.

    SSH is a diagnosis path only (state-backend.md §1): the box is never
    configured by hand, and every change to it is a run of the stack. This
    exists so that reading a log does not start with looking up an address.

    **The host key is pinned, and first contact is not trust-on-first-use.**
    The pin is the public half committed beside the Butane template, which
    the stack refuses to plan without and renders the box's key against
    (`committed.host_key`), so it needs no credential and no call to OCI. It
    goes into a `known_hosts` file of the tool's own that the client is
    pointed at exclusively, with no client configuration of any kind in force
    (`pin_options`), rewritten on every exec.

    Replaces this process rather than wrapping it, so an interactive session
    gets a real terminal and the exit status is ssh's own.
    """
    address = settings.ADDRESS
    pin = committed.host_key()
    known_hosts = config.write_known_hosts(workstation.bundle_dir(), address=address, public_key=pin)
    argv = ['ssh', *pin_options(known_hosts), f'core@{address}', *command]
    # `os.execvp` replaces this process, so what a failure means has to be
    # said before the connection rather than after it.
    log.info(
        'pinning %s to the host key committed in %s (%s); a refusal means the box answering does not hold '
        'it -- a box the stack has not replaced since the key was generated, or something interposed on the path',
        address,
        committed.HOST_KEY.name,
        pin,
    )
    log.info('%s', ' '.join(argv))
    os.execvp('ssh', argv)


#: What a registry names a manifest by, and the whole of what this reads off
#: the HEAD response.
DIGEST_HEADER = 'Docker-Content-Digest'


def _image_digest(repository: str, reference: str) -> str:
    """The digest `reference` -- a tag, or a digest -- resolves to in `repository`, via the registry API.

    Anonymous pull scope is enough to read a manifest, so this needs no
    credential -- which is the point: the check has to run on a PR from a
    fork's CI as readily as on main. A reference the registry does not serve
    raises the registry's `HTTPError`.
    """
    image = f'{repository}@{reference}' if reference.startswith('sha256:') else f'{repository}:{reference}'
    repository = repository.removeprefix('docker.io/')
    log.info('asking the registry what %s resolves to', image)

    token_url = f'https://auth.docker.io/token?service=registry.docker.io&scope=repository:{repository}:pull'
    with urllib.request.urlopen(token_url, timeout=60) as response:
        token = _string(json.load(response), 'token', what=f'the registry pull token for {repository}')

    request = urllib.request.Request(
        f'https://registry-1.docker.io/v2/{repository}/manifests/{reference}',
        method='HEAD',
        headers={
            'Authorization': f'Bearer {token}',
            'Accept': 'application/vnd.oci.image.index.v1+json,'
            'application/vnd.docker.distribution.manifest.list.v2+json,'
            'application/vnd.oci.image.manifest.v1+json,'
            'application/vnd.docker.distribution.manifest.v2+json',
        },
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        # `.get`, because a header this asks for and does not receive is the
        # case below rather than a `KeyError` from inside a mapping.
        digest = response.headers.get(DIGEST_HEADER, '')
    if not digest:
        # An empty answer would otherwise be logged as a resolution, and the
        # check that exists to catch a bad pin would report success.
        raise RuntimeError(f'the registry answered for {image} without a {DIGEST_HEADER} header')
    return str(digest)


def _postgres_pin_ok() -> bool:
    """Whether the registry serves the Postgres pin's digest; that the tag has moved past it is said, not failed."""
    try:
        pinned = _image_digest(settings.POSTGRES_REPOSITORY, settings.POSTGRES_DIGEST)
        current = _image_digest(settings.POSTGRES_REPOSITORY, settings.POSTGRES_TAG)
    except (urllib.error.HTTPError, RuntimeError) as exc:
        log.error('%s: registry says %s (is the digest published?)', settings.POSTGRES_IMAGE, exc)
        return False
    if pinned != settings.POSTGRES_DIGEST:
        log.error('%s: the registry serves the pin as %s', settings.POSTGRES_IMAGE, pinned)
        return False
    if current == pinned:
        log.info('%s is the tag %s as the registry serves it now', pinned, settings.POSTGRES_TAG)
    else:
        # Not a failure: the tag is rebuilt for every minor release and base
        # image, and renovate's monthly digest update moves the pin
        # (settings.py). The box runs the pinned image until then.
        log.info(
            'the registry serves the pinned %s; the tag %s has since moved to %s',
            pinned,
            settings.POSTGRES_TAG,
            current,
        )
    return True


def verify_pins() -> bool:
    """Check the pinned artifacts are what settings.py claims they are.

    Renovate can bump a version but cannot compute the tarball's digest, and
    a digest written by hand can name an image nobody published, so this runs
    in CI on every PR: a bump that leaves AGE_SHA256 stale, or a Postgres pin
    whose digest the registry does not serve, fails here rather than at first
    boot -- where the first strands the appliance without an encryptor and
    the second leaves it without a database.
    """
    ok = True

    log.info('downloading age %s to hash it against its pin: %s', settings.AGE_VERSION, settings.AGE_URL)
    hasher = hashlib.sha256()
    with urllib.request.urlopen(settings.AGE_URL, timeout=300) as response:
        for chunk in iter(lambda: response.read(1 << 20), b''):
            hasher.update(chunk)
    age_digest = hasher.hexdigest()
    if age_digest != settings.AGE_SHA256:
        log.error('age: pinned %s, actual %s', settings.AGE_SHA256, age_digest)
        ok = False
    else:
        log.info('age %s matches its pin', settings.AGE_VERSION)

    ok = _postgres_pin_ok() and ok

    # The stream publishes its current release alone, so the pinned digest is
    # checked against it while the pin is that release; an older pin is held
    # by the upload, which checks the download against it.
    current = fcos_artifact()
    if current.release == settings.FCOS_RELEASE and current.sha256 != settings.FCOS_ARTIFACT_SHA256:
        log.error(
            'FCOS %s: pinned %s, the stream publishes %s',
            settings.FCOS_RELEASE,
            settings.FCOS_ARTIFACT_SHA256,
            current.sha256,
        )
        ok = False
    else:
        log.info('FCOS %s stream is at %s; the pin is %s', settings.FCOS_STREAM, current.release, settings.FCOS_RELEASE)
    return ok
