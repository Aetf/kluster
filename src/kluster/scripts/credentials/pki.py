"""The state-backend's PKI: one escrowed CA key, three kinds of re-issuable leaf.

A single-purpose private CA that signs three kinds of certificate — the
server's and the `ci`/`operator` clients' (physical/state-backend.md §3).

**The CA key is the only escrowed half.** It is random at creation and its
ciphertext lives under `escrow/state-backend/ca` (credentials.md §2.2); losing
it costs a new CA, which is a reissued server certificate, a replacement of
the box and a fresh bundle for every client. **Leaf keys are random at issuance and escrowed nowhere**:
they are re-issuable from the CA, so keeping a copy would add an exposure that
buys back nothing. Issuing one twice therefore produces two different keys,
which is why a caller that needs a certificate and its key takes both halves
from a single `issue_*` call.

P-256 rather than RSA: the live CA is a P-256 key, and re-keying it is a
new CA and a replacement of the appliance rather than an edit here.

**Every certificate names its key, and every leaf names its issuer's.** The
CA certificate carries a Subject Key Identifier, and each leaf an Authority
Key Identifier equal to it, beside a Subject Key Identifier of its own.
OpenSSL's strict verification (`VERIFY_X509_STRICT`, which Python's default
TLS context sets from 3.13) refuses a chain without the first two: a leaf
lacking the Authority Key Identifier, and a CA certificate lacking the
Subject Key Identifier even under a leaf that has one. Both identifiers are
the SHA-1 of the CA's public key (RFC 5280 §4.2.1.2, method 1) and the
Authority Key Identifier carries nothing else, no issuer name or serial:
the CA certificate is rendered afresh with a new serial each time, and a
leaf has to keep chaining to every rendering of it.

**Every certificate is valid from `NOT_BEFORE_MARGIN` before it is issued.**
A verifier holds `notBefore` against its own clock, which can trail the
issuer's. The box and a CI runner check what a workstation issued, with
clocks off from it by whatever NTP leaves, more on a host whose clock has
not converged. On the issuing host itself, OpenSSL reads `time()`, which on
Linux is the coarse clock and still reads the previous second for a few
milliseconds after each second begins, while `notBefore` is the issuance
second rounded down, so a certificate issued then and verified at once is
"not yet valid" there. The margin is cfssl's default backdate, five minutes
(step-ca uses one): it absorbs both with room to spare, and costs nothing,
because no leaf is revoked (physical/state-backend.md §3) and the expiry,
which the renewal and alert margins are held against, is still counted from
the issuance.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
from dataclasses import dataclass, field
from typing import Annotated

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from kluster.lib.secret import Secret

CURVE = ec.SECP256R1()

#: Long enough that the CA is not an operational concern, short enough to be a
#: real bound; the three leaves below rotate far more often.
CA_VALIDITY = dt.timedelta(days=3653)
LEAF_VALIDITY = dt.timedelta(days=1096)

#: How long before its issuance a certificate is already valid: the room a
#: verifier whose clock runs behind the issuer's is given (module docstring).
NOT_BEFORE_MARGIN = dt.timedelta(minutes=5)

#: The appliance's pooler maps a client certificate's Common Name to the
#: Postgres role it connects as.
CLIENT_NAMES = ('ci', 'operator')

CA_COMMON_NAME = 'kluster state-backend CA'


@dataclass(frozen=True)
class Credential:
    """A private key with its certificate, in the PEM forms consumers want.

    The key does not print; the certificate is public and does.
    """

    key_pem: Annotated[bytes, Secret] = field(repr=False, compare=False)
    cert_pem: bytes


def _pem_key(key: ec.EllipticCurvePrivateKey) -> bytes:
    return key.private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )


def _at(now: dt.datetime | None) -> dt.datetime:
    """The moment a certificate is issued at, defaulted the one way.

    Every `issue_*` takes an optional clock so a test can pin validity; the
    default belongs in one place rather than beside each of them.
    """
    return now or dt.datetime.now(dt.UTC)


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def _builder(
    common_name: str, public_key: ec.EllipticCurvePublicKey, now: dt.datetime, validity: dt.timedelta
) -> x509.CertificateBuilder:
    """What every certificate the CA signs starts from: the CA as its issuer, and the validity of one issued at `now`.

    The builder takes `notBefore` once and refuses a second, so a
    certificate started here keeps `NOT_BEFORE_MARGIN`.
    """
    return (
        x509.CertificateBuilder()
        .subject_name(_name(common_name))
        .issuer_name(_name(CA_COMMON_NAME))
        .public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - NOT_BEFORE_MARGIN)
        .not_valid_after(now + validity)
    )


def generate_ca_key() -> str:
    """A fresh CA private key as PKCS8 PEM — what the CA row's `generate` escrows.

    Text rather than bytes because the escrow's unit is a plaintext string:
    one registry, one shape, whether the secret is a passphrase or a key.
    """
    return _pem_key(ec.generate_private_key(CURVE)).decode()


def not_valid_after(cert_pem: bytes) -> dt.datetime:
    """When a PEM certificate stops being valid, in UTC."""
    return x509.load_pem_x509_certificate(cert_pem).not_valid_after_utc


@dataclass(frozen=True, eq=False)
class Authority:
    """The CA, in hand: the recovered private key and what it will sign.

    Certificates are re-issued rather than reproduced byte for byte — serial
    numbers are random and validity is counted from the issuance — so two
    runs produce two certificates that assert the same thing. What
    identifies the CA across them is its public key, which is stable because
    the private half comes from escrow.

    The key does not print. Today's type carries `object`'s repr and would
    disclose nothing, but that is a property of the library rather than of
    this record, and what the field holds is the escrowed private key.
    """

    key: Annotated[ec.EllipticCurvePrivateKey, Secret] = field(repr=False, compare=False)

    @classmethod
    def from_pem(cls, pem: str | bytes) -> Authority:
        data = pem.encode() if isinstance(pem, str) else pem
        key = serialization.load_pem_private_key(data, password=None)
        if not isinstance(key, ec.EllipticCurvePrivateKey):
            raise ValueError(f'the escrowed CA key is a {type(key).__name__}, not an elliptic-curve key')
        return cls(key=key)

    @property
    def key_pem(self) -> bytes:
        return _pem_key(self.key)

    def certificate(self, *, now: dt.datetime | None = None) -> Credential:
        """The self-signed CA certificate, with the key that signed it."""
        cert = (
            _builder(CA_COMMON_NAME, self.key.public_key(), _at(now), CA_VALIDITY)
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
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(self.key.public_key()), critical=False)
            .sign(self.key, hashes.SHA256())
        )
        return Credential(key_pem=self.key_pem, cert_pem=cert.public_bytes(serialization.Encoding.PEM))

    def _leaf(
        self,
        *,
        common_name: str,
        usage: x509.ExtendedKeyUsage,
        san: x509.SubjectAlternativeName | None,
        now: dt.datetime,
    ) -> Credential:
        key = ec.generate_private_key(CURVE)
        builder = (
            _builder(common_name, key.public_key(), now, LEAF_VALIDITY)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(usage, critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(self.key.public_key()), critical=False)
            .add_extension(x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False)
        )
        if san is not None:
            builder = builder.add_extension(san, critical=False)
        cert = builder.sign(self.key, hashes.SHA256())
        return Credential(key_pem=_pem_key(key), cert_pem=cert.public_bytes(serialization.Encoding.PEM))

    def issue_server(self, address: str, *, now: dt.datetime | None = None) -> Credential:
        """The Postgres server certificate.

        Its SAN is the reserved public IP as a literal: clients connect with
        `sslmode=verify-full` by IP, keeping the state backend's hot path free
        of any DNS dependency.
        """
        return self._leaf(
            common_name=address,
            usage=x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
            san=x509.SubjectAlternativeName([x509.IPAddress(ipaddress.ip_address(address))]),
            now=_at(now),
        )

    def issue_client(self, name: str, *, now: dt.datetime | None = None) -> Credential:
        """A client certificate; its Common Name is the Postgres role it becomes."""
        if name not in CLIENT_NAMES:
            raise ValueError(f'unknown client {name!r}; the CA issues to {CLIENT_NAMES}')
        return self._leaf(
            common_name=name,
            usage=x509.ExtendedKeyUsage([ExtendedKeyUsageOID.CLIENT_AUTH]),
            san=None,
            now=_at(now),
        )
