"""The state-backend PKI: one escrowed CA, three leaves it can always re-issue.

The properties worth holding are the ones a silent break would hide — that a
CA recovered from its PEM is the same CA, that a leaf gets a *fresh* key every
time (escrowing leaves is what this design deliberately does not do), and that
openssl itself accepts the chain.
"""

import ipaddress
import subprocess as sp
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa

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


def test_the_generated_ca_key_is_p256(authority: pki.Authority) -> None:
    assert isinstance(authority.key.curve, ec.SECP256R1)


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


def test_clients_have_distinct_keys(authority: pki.Authority) -> None:
    assert authority.issue_client('ci').key_pem != authority.issue_client('operator').key_pem


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
    ca = x509.load_pem_x509_certificate(authority.certificate().cert_pem)
    for credential in (authority.issue_server(ADDRESS), authority.issue_client('operator')):
        x509.load_pem_x509_certificate(credential.cert_pem).verify_directly_issued_by(ca)


def test_a_re_issued_ca_certificate_still_verifies_an_older_leaf(authority: pki.Authority) -> None:
    # The CA certificate is re-created on every render (new serial, new
    # validity window) while its key stays put, so a bundle written on Monday
    # has to chain to the certificate rendered on Tuesday.
    leaf = x509.load_pem_x509_certificate(authority.issue_client('operator').cert_pem)

    later = x509.load_pem_x509_certificate(authority.certificate().cert_pem)

    leaf.verify_directly_issued_by(later)


def test_ca_is_a_ca_and_leaves_are_not(authority: pki.Authority) -> None:
    ca = x509.load_pem_x509_certificate(authority.certificate().cert_pem)
    leaf = x509.load_pem_x509_certificate(authority.issue_client('ci').cert_pem)
    assert ca.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    assert not leaf.extensions.get_extension_for_class(x509.BasicConstraints).value.ca


def test_openssl_accepts_the_chain(authority: pki.Authority, tmp_path: Path) -> None:
    ca_file = tmp_path / 'ca.pem'
    server_file = tmp_path / 'server.pem'
    _ = ca_file.write_bytes(authority.certificate().cert_pem)
    _ = server_file.write_bytes(authority.issue_server(ADDRESS).cert_pem)
    proc = sp.run(
        ['openssl', 'verify', '-CAfile', str(ca_file), str(server_file)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
