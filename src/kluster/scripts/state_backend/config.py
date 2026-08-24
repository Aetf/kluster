"""Rendering the appliance's Ignition, and the client bundle that talks to it.

The Butane template is the box; this module supplies the values that only
exist at provision time — the reserved IP the server certificate is issued
for, the write-only B2 credential, the derived PKI and age recipients — and
hands the result to `butane` for validation and conversion.
"""

from __future__ import annotations

import logging
import subprocess as sp
from dataclasses import dataclass
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, StrictUndefined

from kluster.scripts.credentials import age, pki

from . import settings

log = logging.getLogger(__name__)

#: `deploy/` sits outside the package: it is deployment material, not library
#: code, and the appliance's definition is meant to be readable on its own.
DEPLOY_DIR = Path(__file__).resolve().parents[4] / 'deploy' / 'state-backend'

TEMPLATE = 'butane.yaml.j2'
DUMP_SCRIPT = 'state-dump.py'
OPERATOR_KEYS = 'operator-keys.txt'


@dataclass(frozen=True)
class ClientBundle:
    """What an operator or CI needs to reach the backend."""

    ca_cert: bytes
    cert: bytes
    key: bytes
    url: str


def operator_keys() -> list[str]:
    lines = (DEPLOY_DIR / OPERATOR_KEYS).read_text().splitlines()
    return [line.strip() for line in lines if line.strip() and not line.startswith('#')]


def render_ignition(root: bytes, *, address: str, dump_key_id: str, dump_key: str, bucket_id: str) -> str:
    """Butane in, validated Ignition out."""
    environment = Environment(
        loader=FileSystemLoader(DEPLOY_DIR),
        undefined=StrictUndefined,
        keep_trailing_newline=True,
    )
    recipients = [
        age.generation(root, settings.AGE_GENERATION).public,
        age.generation(root, settings.AGE_GENERATION - 1).public,
    ]
    butane = environment.get_template(TEMPLATE).render(
        operator_keys=operator_keys(),
        postgres_uid=settings.POSTGRES_UID,
        postgres_image=settings.POSTGRES_IMAGE,
        database=settings.DATABASE,
        ci_role=settings.CI_ROLE,
        operator_role=settings.OPERATOR_ROLE,
        ca_cert=pki.ca_credential(root).cert_pem.decode().strip(),
        server_cert=pki.server_credential(root, address).cert_pem.decode().strip(),
        server_key=pki.server_credential(root, address).key_pem.decode().strip(),
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


def client_bundle(root: bytes, *, name: str, address: str) -> ClientBundle:
    """The `ci` or `operator` credential, with the URL that uses it.

    `verify-full` against a literal IP: the state backend's hot path must not
    depend on DNS, which is itself something this backend deploys.
    """
    credential = pki.client_credential(root, name)
    url = (
        f'postgres://{name}@{address}:{settings.PORT}/{settings.DATABASE}'
        '?sslmode=verify-full&sslrootcert=$KLUSTER_PG_CA'
        '&sslcert=$KLUSTER_PG_CERT&sslkey=$KLUSTER_PG_KEY'
    )
    return ClientBundle(
        ca_cert=pki.ca_credential(root).cert_pem,
        cert=credential.cert_pem,
        key=credential.key_pem,
        url=url,
    )


def write_client_bundle(bundle: ClientBundle, directory: Path) -> None:
    """Place a bundle on disk with the permissions libpq insists on."""
    directory.mkdir(parents=True, exist_ok=True)
    _ = (directory / 'ca.crt').write_bytes(bundle.ca_cert)
    _ = (directory / 'client.crt').write_bytes(bundle.cert)
    key_path = directory / 'client.key'
    _ = key_path.write_bytes(bundle.key)
    key_path.chmod(0o600)
    _ = (directory / 'backend-url').write_text(bundle.url + '\n')
    log.info('wrote client bundle to %s', directory)
