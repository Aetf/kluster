"""Rendering the appliance's Ignition, and the client bundle that talks to it.

The Butane template is the box; this module supplies the values that only
exist at provision time — the reserved IP the server certificate is issued
for, the write-only B2 credential, the derived PKI and age recipients — and
hands the result to `butane` for validation and conversion.
"""

from __future__ import annotations

import hashlib
import json
import logging
import subprocess as sp
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from jinja2 import Environment, FileSystemLoader, StrictUndefined

from kluster.scripts.credentials import age, pki, workstation

from . import settings

log = logging.getLogger(__name__)

#: `deploy/` sits outside the package: it is deployment material, not library
#: code, and the appliance's definition is meant to be readable on its own.
DEPLOY_DIR = Path(__file__).resolve().parents[4] / 'deploy' / 'state-backend'

TEMPLATE = 'butane.yaml.j2'
DUMP_SCRIPT = 'state-dump.py'
OPERATOR_KEYS = 'operator-keys.txt'

#: The bundle's file names, shared by the writer and the URL that names them.
CA_FILE = 'ca.crt'
CERT_FILE = 'client.crt'
KEY_FILE = 'client.key'
URL_FILE = 'backend-url'


@dataclass(frozen=True)
class ClientBundle:
    """What an operator or CI needs to reach the backend."""

    name: str
    address: str
    ca_cert: bytes
    cert: bytes
    key: bytes

    def url(self, directory: Path) -> str:
        """The connection string, pointing at this bundle on disk.

        The paths are written out rather than left as `$KLUSTER_PG_CA`-style
        references: libpq does not expand variables inside a connection
        string, and neither does the driver Pulumi's Postgres backend uses --
        a placeholder reaches `open()` verbatim and fails as a missing file.

        `verify-full` against a literal IP: the state backend's hot path must
        not depend on DNS, which is itself something this backend deploys.
        """
        directory = directory.resolve()
        return (
            f'postgres://{self.name}@{self.address}:{settings.PORT}/{settings.DATABASE}'
            f'?sslmode=verify-full&sslrootcert={directory / CA_FILE}'
            f'&sslcert={directory / CERT_FILE}&sslkey={directory / KEY_FILE}'
        )


def operator_keys() -> list[str]:
    lines = (DEPLOY_DIR / OPERATOR_KEYS).read_text().splitlines()
    return [line.strip() for line in lines if line.strip() and not line.startswith('#')]


def machine(seed: bytes, *, address: str, dump_key_id: str, dump_key: str, bucket_id: str) -> dict[str, Any]:
    """Everything the Butane template needs: the machine, as values.

    Named rather than inlined because two things read it -- the renderer, and
    the digest that decides whether a running box still matches the
    repository (`digests`). A field the template uses but this does not carry
    would be a change the converge cannot see.
    """
    recipients = [
        age.generation(seed, settings.AGE_GENERATION).public,
        age.generation(seed, settings.AGE_GENERATION - 1).public,
    ]
    return dict(
        operator_keys=operator_keys(),
        postgres_uid=settings.POSTGRES_UID,
        postgres_image=settings.POSTGRES_IMAGE,
        database=settings.DATABASE,
        ci_role=settings.CI_ROLE,
        operator_role=settings.OPERATOR_ROLE,
        ca_cert=pki.ca_credential(seed).cert_pem.decode().strip(),
        server_cert=pki.server_credential(seed, address).cert_pem.decode().strip(),
        server_key=pki.server_credential(seed, address).key_pem.decode().strip(),
        age_recipients=recipients,
        age_url=settings.AGE_URL,
        age_sha256=settings.AGE_SHA256,
        b2_dump_key_id=dump_key_id,
        b2_dump_key=dump_key,
        b2_bucket_id=bucket_id,
        b2_prefix=settings.B2_PREFIX,
        dump_script=(DEPLOY_DIR / DUMP_SCRIPT).read_text().strip(),
        dump_schedule=settings.DUMP_SCHEDULE,
        reboot_day=settings.REBOOT_DAY,
        reboot_time=settings.REBOOT_TIME,
        reboot_window_minutes=settings.REBOOT_WINDOW_MINUTES,
    )


def render_ignition(seed: bytes, *, address: str, dump_key_id: str, dump_key: str, bucket_id: str) -> str:
    """Butane in, validated Ignition out."""
    environment = Environment(
        loader=FileSystemLoader(DEPLOY_DIR),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )
    butane = environment.get_template(TEMPLATE).render(
        **machine(seed, address=address, dump_key_id=dump_key_id, dump_key=dump_key, bucket_id=bucket_id)
    )
    log.info('handing %s to butane for validation and conversion to Ignition', TEMPLATE)
    proc = sp.run(
        ['butane', '--strict', '--pretty'],
        input=butane,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.returncode != 0:
        raise RuntimeError(f'butane rejected the config:\n{proc.stderr}')
    return proc.stdout


#: What the digest never sees. The dump key's secret is a credential, and
#: putting even its hash in cloud metadata buys nothing: the key's *identity*
#: is what the converge compares, and that is `b2_dump_key_id`.
_UNDIGESTED = frozenset({'b2_dump_key'})

#: Certificates are re-issued on every render, so their bytes differ run to
#: run while the machine does not (pki.py). These are compared by what they
#: assert instead: subject, public key, and names.
_CREDENTIALS = frozenset({'ca_cert', 'server_cert', 'server_key'})


def _identity(pem: str) -> str:
    """A certificate or private key reduced to what it *is*.

    Validity dates and signature bytes move on every issuance; the subject,
    the public key and the SANs do not. Digesting the latter is what lets a
    re-render be recognised as the same machine.
    """
    data = pem.encode()
    if 'CERTIFICATE' in pem:
        cert = x509.load_pem_x509_certificate(data)
        try:
            names = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
            san = sorted(str(name.value) for name in names)
        except x509.ExtensionNotFound:
            san = []
        public = cert.public_key()
        subject = cert.subject.rfc4514_string()
    else:
        public = serialization.load_pem_private_key(data, password=None).public_key()
        subject, san = '', []
    spki = public.public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    return json.dumps([subject, san, hashlib.sha256(spki).hexdigest()])


def digests(seed: bytes, *, address: str, dump_key_id: str, bucket_id: str) -> dict[str, str]:
    """A digest per component of the machine, for comparing a box to the repo.

    Per component rather than one number so that a drifted box can say *what*
    drifted -- `butane`, `operator_keys`, `postgres_image` -- which is the
    difference between "re-provision, trust me" and a converge whose reason is
    readable. The map is small enough to travel in the instance's metadata,
    which is where the answer for a running box comes from.

    The template itself is a component: it is the machine's definition, and a
    change to it must be visible without every value it interpolates changing.
    """
    values = machine(seed, address=address, dump_key_id=dump_key_id, dump_key='', bucket_id=bucket_id)
    parts = {'butane': (DEPLOY_DIR / TEMPLATE).read_text()}
    for key, value in values.items():
        if key in _UNDIGESTED:
            continue
        parts[key] = _identity(value) if key in _CREDENTIALS else json.dumps(value, sort_keys=True, default=str)
    return {key: hashlib.sha256(value.encode()).hexdigest()[:16] for key, value in sorted(parts.items())}


def drift(intended: dict[str, str], actual: dict[str, str]) -> list[str]:
    """The component names that differ. An empty list means the box matches.

    A component the box does not carry counts as drift, so a box provisioned
    before a component existed -- or before this bookkeeping did -- converges
    rather than passing on a silence.
    """
    return sorted(key for key in set(intended) | set(actual) if intended.get(key) != actual.get(key))


def client_bundle(seed: bytes, *, name: str, address: str) -> ClientBundle:
    """The `ci` or `operator` credential. The URL comes from where it lands."""
    credential = pki.client_credential(seed, name)
    return ClientBundle(
        name=name,
        address=address,
        ca_cert=pki.ca_credential(seed).cert_pem,
        cert=credential.cert_pem,
        key=credential.key_pem,
    )


def write_client_bundle(bundle: ClientBundle, directory: Path) -> None:
    """Place a bundle on disk with the permissions libpq insists on.

    libpq refuses a client key that anyone but its owner can read, so that one
    is `0600`; the directory is `0700` for the same reason one level up, which
    is what `workstation.secret_dir` gives every slot.
    """
    _ = workstation.secret_dir(directory)
    _ = (directory / CA_FILE).write_bytes(bundle.ca_cert)
    _ = (directory / CERT_FILE).write_bytes(bundle.cert)
    key_path = directory / KEY_FILE
    _ = key_path.write_bytes(bundle.key)
    key_path.chmod(0o600)
    _ = (directory / URL_FILE).write_text(bundle.url(directory) + '\n')
    log.info('wrote client bundle to %s', directory)
