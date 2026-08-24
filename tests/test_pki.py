"""The state-backend PKI is derived, so its properties are checked rather than
assumed: same seed same key, a chain openssl itself accepts, and a server
certificate carrying the IP a verify-full client will match."""

import ipaddress
import subprocess as sp
from pathlib import Path

import pytest
from cryptography import x509
from cryptography.hazmat.primitives.asymmetric import ec

from kluster.scripts.credentials import pki

ROOT = bytes(range(32))
ADDRESS = '192.0.2.10'


def test_keys_are_deterministic() -> None:
    assert pki.server_credential(ROOT, ADDRESS).key_pem == pki.server_credential(ROOT, ADDRESS).key_pem


def test_roots_separate_keys() -> None:
    other = bytes(range(1, 33))
    assert pki.server_credential(ROOT, ADDRESS).key_pem != pki.server_credential(other, ADDRESS).key_pem


def test_clients_have_distinct_keys() -> None:
    assert pki.client_credential(ROOT, 'ci').key_pem != pki.client_credential(ROOT, 'operator').key_pem


def test_server_certificate_carries_the_ip() -> None:
    cert = x509.load_pem_x509_certificate(pki.server_credential(ROOT, ADDRESS).cert_pem)
    san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
    assert san.get_values_for_type(x509.IPAddress) == [ipaddress.ip_address(ADDRESS)]


def test_client_common_name_is_the_role() -> None:
    cert = x509.load_pem_x509_certificate(pki.client_credential(ROOT, 'ci').cert_pem)
    assert cert.subject.rfc4514_string() == 'CN=ci'


def test_unknown_client_is_rejected() -> None:
    with pytest.raises(ValueError):
        _ = pki.client_credential(ROOT, 'root')


def test_leaves_are_issued_by_the_ca() -> None:
    ca = x509.load_pem_x509_certificate(pki.ca_credential(ROOT).cert_pem)
    for credential in (pki.server_credential(ROOT, ADDRESS), pki.client_credential(ROOT, 'operator')):
        x509.load_pem_x509_certificate(credential.cert_pem).verify_directly_issued_by(ca)


def test_ca_is_a_ca_and_leaves_are_not() -> None:
    ca = x509.load_pem_x509_certificate(pki.ca_credential(ROOT).cert_pem)
    leaf = x509.load_pem_x509_certificate(pki.client_credential(ROOT, 'ci').cert_pem)
    assert ca.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    assert not leaf.extensions.get_extension_for_class(x509.BasicConstraints).value.ca


def test_key_is_p256() -> None:
    key = pki.ca_key(ROOT)
    assert isinstance(key.curve, ec.SECP256R1)


def test_openssl_accepts_the_chain(tmp_path: Path) -> None:
    ca_file = tmp_path / 'ca.pem'
    server_file = tmp_path / 'server.pem'
    _ = ca_file.write_bytes(pki.ca_credential(ROOT).cert_pem)
    _ = server_file.write_bytes(pki.server_credential(ROOT, ADDRESS).cert_pem)
    proc = sp.run(
        ['openssl', 'verify', '-CAfile', str(ca_file), str(server_file)],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
