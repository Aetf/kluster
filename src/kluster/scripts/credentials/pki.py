"""The state-backend's PKI, derived from the derivation seed.

A single-purpose private CA with exactly three certificates under it — the
server and the `ci`/`operator` clients (physical/state-backend.md §3). Nothing
here is escrowed: every private key is an HKDF derivation of the derivation
seed, so losing the box (or the CA file) costs a re-provision, not a recovery.

P-256 rather than RSA because a private scalar is a deterministic function of
the seed, which deterministic RSA generation is not (credentials.md §2.2).
Certificates themselves are re-issued rather than reproduced byte-for-byte:
re-provision is the only apply path this appliance has.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
from dataclasses import dataclass

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from . import seeds

CURVE = ec.SECP256R1()

#: Long enough that the CA is not an operational concern, short enough to be a
#: real bound; the three leaves below rotate far more often.
CA_VALIDITY = dt.timedelta(days=3653)
LEAF_VALIDITY = dt.timedelta(days=1096)

#: Postgres maps a client certificate's Common Name to a database user.
CLIENT_NAMES = ('ci', 'operator')


def _private_key(scalar: bytes) -> ec.EllipticCurvePrivateKey:
    """A P-256 key whose scalar is a function of the seed.

    The derived bytes are reduced into [1, n-1] rather than rejected-and-
    retried, so the key exists for every possible seed.
    """
    n = 0xFFFFFFFF00000000FFFFFFFFFFFFFFFFBCE6FAADA7179E84F3B9CAC2FC632551  # P-256 group order
    value = int.from_bytes(scalar, 'big') % (n - 1) + 1
    return ec.derive_private_key(value, CURVE)


def _serial(seed: bytes, label: str) -> int:
    """A stable serial, so re-issuing a certificate does not invent identity."""
    return int.from_bytes(seeds.derive(seed, f'state-backend/serial/{label}', 20), 'big') >> 1


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


@dataclass(frozen=True)
class Credential:
    """A private key with its certificate, in the PEM forms consumers want."""

    key_pem: bytes
    cert_pem: bytes


def _pem_key(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def ca_key(seed: bytes) -> ec.EllipticCurvePrivateKey:
    return _private_key(seeds.ca_scalar(seed))


def ca_credential(seed: bytes, *, now: dt.datetime | None = None) -> Credential:
    now = now or dt.datetime.now(dt.timezone.utc)
    key = ca_key(seed)
    name = _name('kluster state-backend CA')
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(_serial(seed, 'ca'))
        .not_valid_before(now)
        .not_valid_after(now + CA_VALIDITY)
        .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False,
                content_commitment=False,
                key_encipherment=False,
                data_encipherment=False,
                key_agreement=False,
                key_cert_sign=True,
                crl_sign=True,
                encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )
    return Credential(key_pem=_pem_key(key), cert_pem=cert.public_bytes(serialization.Encoding.PEM))


def _leaf(
    seed: bytes,
    *,
    label: str,
    common_name: str,
    usage: x509.ExtendedKeyUsage,
    san: x509.SubjectAlternativeName | None,
    now: dt.datetime,
) -> Credential:
    key = _private_key(seeds.cert_scalar(seed, label))
    issuer = ca_key(seed)
    builder = (
        x509.CertificateBuilder()
        .subject_name(_name(common_name))
        .issuer_name(_name('kluster state-backend CA'))
        .public_key(key.public_key())
        .serial_number(_serial(seed, label))
        .not_valid_before(now)
        .not_valid_after(now + LEAF_VALIDITY)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(usage, critical=False)
    )
    if san is not None:
        builder = builder.add_extension(san, critical=False)
    cert = builder.sign(issuer, hashes.SHA256())
    return Credential(key_pem=_pem_key(key), cert_pem=cert.public_bytes(serialization.Encoding.PEM))


def server_credential(seed: bytes, address: str, *, now: dt.datetime | None = None) -> Credential:
    """The Postgres server certificate.

    Its SAN is the reserved public IP as a literal: clients connect with
    `sslmode=verify-full` by IP, keeping the state backend's hot path free of
    any DNS dependency.
    """
    return _leaf(
        seed,
        label='server',
        common_name=address,
        usage=x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
        san=x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(address))]),
        now=now or dt.datetime.now(dt.timezone.utc),
    )


def client_credential(seed: bytes, name: str, *, now: dt.datetime | None = None) -> Credential:
    """A client certificate; its Common Name is the Postgres role it becomes."""
    if name not in CLIENT_NAMES:
        raise ValueError(f'unknown client {name!r}; the CA issues to {CLIENT_NAMES}')
    return _leaf(
        seed,
        label=f'client/{name}',
        common_name=name,
        usage=x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]),
        san=None,
        now=now or dt.datetime.now(dt.timezone.utc),
    )
