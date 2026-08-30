"""The state-backend's PKI: one escrowed CA key, three re-issuable leaves.

A single-purpose private CA with exactly three certificates under it — the
server and the `ci`/`operator` clients (physical/state-backend.md §3).

**The CA key is the only escrowed half.** It is random at creation and its
ciphertext lives under `escrow/state-backend/ca` (credentials.md §2.2); losing
it costs a new CA, which is a re-provision and a redistribution of all three
client bundles. **Leaf keys are random at issuance and escrowed nowhere**:
they are re-issuable from the CA, so keeping a copy would add an exposure that
buys back nothing. Issuing one twice therefore produces two different keys,
which is why a caller that needs a certificate and its key takes both halves
from a single `issue_*` call.

P-256 rather than RSA: the live CA is a P-256 key, and re-keying it is a
re-provision of the appliance rather than an edit here.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
from dataclasses import dataclass

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

CURVE = ec.SECP256R1()

#: Long enough that the CA is not an operational concern, short enough to be a
#: real bound; the three leaves below rotate far more often.
CA_VALIDITY = dt.timedelta(days=3653)
LEAF_VALIDITY = dt.timedelta(days=1096)

#: Postgres maps a client certificate's Common Name to a database user.
CLIENT_NAMES = ('ci', 'operator')

CA_COMMON_NAME = 'kluster state-backend CA'


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


def _at(now: dt.datetime | None) -> dt.datetime:
    """The moment a certificate is issued at, defaulted the one way.

    Every `issue_*` takes an optional clock so a test can pin validity; the
    default belongs in one place rather than beside each of them.
    """
    return now or dt.datetime.now(dt.timezone.utc)


def _name(common_name: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])


def generate_ca_key() -> str:
    """A fresh CA private key as PKCS8 PEM — what `escrow generate` stores.

    Text rather than bytes because the escrow's unit is a plaintext string:
    one registry, one shape, whether the secret is a passphrase or a key.
    """
    return _pem_key(ec.generate_private_key(CURVE)).decode()


@dataclass(frozen=True, eq=False)
class Authority:
    """The CA, in hand: the recovered private key and what it will sign.

    Certificates are re-issued rather than reproduced byte for byte — serial
    numbers are random and validity starts now — so two runs produce two
    certificates that assert the same thing. What identifies the CA across
    them is its public key, which is stable because the private half comes
    from escrow.
    """

    key: ec.EllipticCurvePrivateKey

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
        now = _at(now)
        name = _name(CA_COMMON_NAME)
        cert = (
            x509.CertificateBuilder()
            .subject_name(name)
            .issuer_name(name)
            .public_key(self.key.public_key())
            .serial_number(x509.random_serial_number())
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
            x509.CertificateBuilder()
            .subject_name(_name(common_name))
            .issuer_name(_name(CA_COMMON_NAME))
            .public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now)
            .not_valid_after(now + LEAF_VALIDITY)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(usage, critical=False)
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
