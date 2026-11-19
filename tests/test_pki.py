"""The state-backend PKI: one escrowed CA, and leaves it can always re-issue.

The properties worth holding are the ones a silent break would hide — that a
CA recovered from its PEM is the same CA, that a leaf gets a *fresh* key every
time (escrowing leaves is what this design deliberately does not do), that
openssl itself accepts the chain, under its strict checks, and that a verifier
whose clock runs behind the issuer's accepts a certificate issued just now.
"""

import datetime as dt
import ipaddress
import subprocess as sp
from pathlib import Path

import process_sessions
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.verification import Criticality, ExtensionPolicy, PolicyBuilder, Store, VerificationError

from kluster.scripts.credentials import pki

ADDRESS = '192.0.2.10'


@pytest.fixture
def authority() -> pki.Authority:
    return pki.Authority.from_pem(pki.generate_ca_key())


def test_a_recovered_ca_is_the_same_ca(authority: pki.Authority) -> None:
    # What comes out of the escrow is a PEM; the CA it names has to be the one
    # every certificate already in the field was signed by.
    again = pki.Authority.from_pem(authority.key_pem)

    assert again.key_pem == authority.key_pem
    assert again.key.public_key().public_numbers() == authority.key.public_key().public_numbers()


def test_a_leaf_key_is_random_at_every_issuance(authority: pki.Authority) -> None:
    # The mint model downstream of the CA: a leaf is re-issued rather than
    # reproduced, which is why no leaf is escrowed.
    assert authority.issue_server(ADDRESS).key_pem != authority.issue_server(ADDRESS).key_pem
    assert authority.issue_client('ci').key_pem != authority.issue_client('ci').key_pem


def test_the_certificate_and_its_key_belong_together(authority: pki.Authority) -> None:
    # One issuance yields one pair. A caller that asked twice and kept one
    # half of each would hand the box a certificate it cannot answer with.
    credential = authority.issue_server(ADDRESS)
    cert = x509.load_pem_x509_certificate(credential.cert_pem)
    private = serialization.load_pem_private_key(credential.key_pem, password=None)
    assert isinstance(private, ec.EllipticCurvePrivateKey)
    public = cert.public_key()
    assert isinstance(public, ec.EllipticCurvePublicKey)

    assert private.public_key().public_numbers() == public.public_numbers()


def test_server_certificate_carries_the_ip(authority: pki.Authority) -> None:
    cert = x509.load_pem_x509_certificate(authority.issue_server(ADDRESS).cert_pem)
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert san.get_values_for_type(x509.IPAddress) == [ipaddress.ip_address(ADDRESS)]


def test_client_common_name_is_the_role(authority: pki.Authority) -> None:
    cert = x509.load_pem_x509_certificate(authority.issue_client('ci').cert_pem)
    assert cert.subject.rfc4514_string() == 'CN=ci'


def test_unknown_client_is_rejected(authority: pki.Authority) -> None:
    with pytest.raises(ValueError, match='unknown client'):
        _ = authority.issue_client('root')


def test_something_that_is_not_an_ec_key_is_rejected() -> None:
    # An escrow entry holding the wrong kind of key fails where it is read,
    # naming what it found, rather than deep inside a signature call.
    rsa_pem = rsa.generate_private_key(public_exponent=65537, key_size=2048).private_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PrivateFormat.PKCS8,
        encryption_algorithm=serialization.NoEncryption(),
    )

    with pytest.raises(ValueError, match='not an elliptic-curve key'):
        _ = pki.Authority.from_pem(rsa_pem)


def test_leaves_are_issued_by_the_ca(authority: pki.Authority) -> None:
    # The CA certificate is re-created on every render (new serial, new
    # validity window) while its key stays put, so a bundle written on Monday
    # has to chain to the certificate rendered on Tuesday: the leaves are
    # issued first and verified against a certificate rendered after them.
    leaves = (authority.issue_server(ADDRESS), authority.issue_client('operator'))

    later = x509.load_pem_x509_certificate(authority.certificate().cert_pem)

    for credential in leaves:
        x509.load_pem_x509_certificate(credential.cert_pem).verify_directly_issued_by(later)


def _extension[T: x509.ExtensionType](pem: bytes, kind: type[T]) -> T:
    return x509.load_pem_x509_certificate(pem).extensions.get_extension_for_class(kind).value


def test_the_ca_certificate_carries_its_key_s_identifier(authority: pki.Authority) -> None:
    # Strict verification refuses a CA certificate without one, even under a
    # leaf that names it. Derived from the key, so every rendering carries
    # the same one.
    ski = _extension(authority.certificate().cert_pem, x509.SubjectKeyIdentifier)

    assert ski == x509.SubjectKeyIdentifier.from_public_key(authority.key.public_key())


@pytest.mark.parametrize('leaf', ['server', 'client'])
def test_every_leaf_names_the_ca_by_its_key_identifier(authority: pki.Authority, leaf: str) -> None:
    # Strict verification refuses a leaf without one. It names the key alone:
    # an issuer serial in it would stop matching the next rendering of the CA.
    aki = _extension(_issue(authority, leaf).cert_pem, x509.AuthorityKeyIdentifier)
    later = _extension(authority.certificate().cert_pem, x509.SubjectKeyIdentifier)

    assert aki.key_identifier == later.digest
    assert aki.authority_cert_issuer is None
    assert aki.authority_cert_serial_number is None


@pytest.mark.parametrize('leaf', ['server', 'client'])
def test_every_leaf_carries_its_own_key_identifier(authority: pki.Authority, leaf: str) -> None:
    pem = _issue(authority, leaf).cert_pem

    ski = _extension(pem, x509.SubjectKeyIdentifier)

    assert ski == x509.SubjectKeyIdentifier.from_public_key(x509.load_pem_x509_certificate(pem).public_key())


def test_ca_is_a_ca_and_leaves_are_not(authority: pki.Authority) -> None:
    ca = x509.load_pem_x509_certificate(authority.certificate().cert_pem)
    leaf = x509.load_pem_x509_certificate(authority.issue_client('ci').cert_pem)
    assert ca.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    assert not leaf.extensions.get_extension_for_class(x509.BasicConstraints).value.ca


#: Each leaf, and the `openssl verify -purpose` it has to pass: the purpose
#: a TLS peer holds the certificate to on its side of the handshake. Every
#: verification here is `-x509_strict`, the checks Python's default TLS
#: context applies from 3.13.
PURPOSES = {'server': 'sslserver', 'client': 'sslclient'}


def _issue(authority: pki.Authority, leaf: str) -> pki.Credential:
    return authority.issue_server(ADDRESS) if leaf == 'server' else authority.issue_client('operator')


def _verify(ca: bytes, leaf: bytes, directory: Path, *purpose: str) -> sp.CompletedProcess[str]:
    ca_file = directory / 'ca.pem'
    leaf_file = directory / 'leaf.pem'
    _ = ca_file.write_bytes(ca)
    _ = leaf_file.write_bytes(leaf)
    return process_sessions.run(
        ['openssl', 'verify', '-x509_strict', *purpose, '-CAfile', str(ca_file), str(leaf_file)],
        text=True,
        timeout=30,
    )


@pytest.mark.parametrize('leaf', list(PURPOSES))
def test_openssl_accepts_the_chain_for_the_leaf_s_purpose(authority: pki.Authority, tmp_path: Path, leaf: str) -> None:
    """A chain that verifies is half of it; the leaf has to be marked for its side of the handshake.

    A server certificate carrying the client purpose chains to the CA just as
    well, and every `verify-full` client refuses the box that serves it --
    while the readiness wait's `s_client -brief`, which checks no purpose,
    still reads the box as up.
    """
    ca = authority.certificate().cert_pem

    proc = _verify(ca, _issue(authority, leaf).cert_pem, tmp_path, '-purpose', PURPOSES[leaf])

    assert proc.returncode == 0, proc.stderr


@pytest.mark.parametrize('leaf', list(PURPOSES))
def test_openssl_refuses_a_leaf_for_the_other_side_s_purpose(
    authority: pki.Authority, tmp_path: Path, leaf: str
) -> None:
    # The control for the case above: a purpose check that accepted anything
    # would pass it too. Each leaf chains, and is refused for the purpose it
    # was not issued for.
    other = next(purpose for name, purpose in PURPOSES.items() if name != leaf)
    ca = authority.certificate().cert_pem
    issued = _issue(authority, leaf).cert_pem

    assert _verify(ca, issued, tmp_path).returncode == 0
    refused = _verify(ca, issued, tmp_path, '-purpose', other)

    assert refused.returncode != 0
    # OpenSSL 3 says `unsuitable`, 1.1 said `unsupported`; the error is 26 in both.
    assert 'certificate purpose' in refused.stdout + refused.stderr


# -- validity at the verifier's clock ------------------------------------------

#: The issuance every case below pins, so that no case reads the wall clock.
ISSUED = dt.datetime(2030, 1, 1, tzinfo=dt.UTC)

#: How far behind the issuer's the verifier's clock runs: past the second a
#: certificate is stamped with, which is what a coarse clock read in the first
#: milliseconds of that second, or a host a little behind, amounts to.
BEHIND = dt.timedelta(seconds=1)

#: Long enough before `ISSUED` that whatever was issued then is valid at any
#: clock the cases below verify at.
ESTABLISHED = ISSUED - dt.timedelta(days=1)


def _verify_at(authority: pki.Authority, fresh: str, at: dt.datetime) -> None:
    """Verify a chain at `at`, with the certificate `fresh` names issued at `ISSUED` and the rest at `ESTABLISHED`.

    `cryptography`'s verifier takes the time to verify at as an argument,
    which is what keeps the clock out of these cases. Its client policy wants
    a SAN on the leaf, which the client leaf has no use for, so that leaf is
    held to the web-PKI defaults with the SAN made optional and nothing else
    relaxed.
    """
    ca = x509.load_pem_x509_certificate(authority.certificate(now=ISSUED if fresh == 'ca' else ESTABLISHED).cert_pem)
    builder = PolicyBuilder().store(Store([ca])).time(at)
    if fresh == 'client':
        leaf = x509.load_pem_x509_certificate(authority.issue_client('ci', now=ISSUED).cert_pem)
        verifier = builder.extension_policies(
            ca_policy=ExtensionPolicy.webpki_defaults_ca(),
            ee_policy=ExtensionPolicy.webpki_defaults_ee().may_be_present(
                x509.SubjectAlternativeName, Criticality.AGNOSTIC, None
            ),
        ).build_client_verifier()
        _ = verifier.verify(leaf, [])
        return
    issued = ISSUED if fresh == 'server' else ESTABLISHED
    leaf = x509.load_pem_x509_certificate(authority.issue_server(ADDRESS, now=issued).cert_pem)
    _ = builder.build_server_verifier(x509.IPAddress(ipaddress.ip_address(ADDRESS))).verify(leaf, [])


@pytest.mark.parametrize('fresh', ['ca', 'server', 'client'])
def test_a_certificate_issued_just_now_is_valid_to_a_clock_behind_the_issuer_s(
    authority: pki.Authority, fresh: str
) -> None:
    # A certificate stamped valid from the second it was issued in is "not
    # yet valid" to OpenSSL on the issuing host itself for the first
    # milliseconds of that second, and to any host whose clock is behind.
    _verify_at(authority, fresh, ISSUED - BEHIND)


@pytest.mark.parametrize('fresh', ['ca', 'server', 'client'])
def test_a_clock_behind_by_more_than_the_margin_finds_the_certificate_not_yet_valid(
    authority: pki.Authority, fresh: str
) -> None:
    # The control for the case above: a verifier that never looked at
    # `notBefore` would pass it too.
    with pytest.raises(VerificationError, match='not valid at validation time'):
        _verify_at(authority, fresh, ISSUED - pki.NOT_BEFORE_MARGIN - BEHIND)
