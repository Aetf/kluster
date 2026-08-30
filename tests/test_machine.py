"""The appliance's bill of materials, which decides whether a box gets rebuilt.

Leaf keys are random at issuance, so the naive digest — hash the rendered
certificate — makes every converge see drift and replace a box that is
perfectly fine. What the digest has to capture instead is what the box *is*:
the CA it chains to, and the address it answers on.
"""

from __future__ import annotations

import shutil
from dataclasses import fields
from pathlib import Path

import pytest
from memory_kit import MemoryKit

from kluster.scripts.credentials import age, escrow, pki
from kluster.scripts.state_backend import config

needs_age = pytest.mark.skipif(shutil.which(age.BINARY) is None, reason='age is not on PATH (mise x -- ...)')

ADDRESS = '192.0.2.10'
OTHER = '192.0.2.11'
RECIPIENT = 'age1exampleexampleexampleexampleexampleexampleexampleexamplezzzz'


@pytest.fixture
def roots() -> config.Roots:
    return config.Roots(ca=pki.Authority.from_pem(pki.generate_ca_key()), age_recipients=(RECIPIENT,))


def _digests(roots: config.Roots, address: str = ADDRESS) -> dict[str, str]:
    return config.digests(roots, address=address, dump_key_id='key-id', bucket_id='bucket-id')


def test_a_re_render_is_not_drift(roots: config.Roots) -> None:
    # Otherwise every converge terminates a healthy box and rebuilds it.
    assert config.drift(_digests(roots), _digests(roots)) == []


def test_the_address_the_server_answers_on_is_drift(roots: config.Roots) -> None:
    # The reserved IP is in the server certificate's SAN, and a client
    # connects to it with verify-full; a box holding the wrong one is unusable.
    assert config.drift(_digests(roots), _digests(roots, OTHER)) == ['server_cert']


def test_a_different_ca_is_drift(roots: config.Roots) -> None:
    # The CA's key comes from escrow and outlives every render, so it is the
    # one certificate compared by public key.
    other = config.Roots(ca=pki.Authority.from_pem(pki.generate_ca_key()), age_recipients=roots.age_recipients)

    assert 'ca_cert' in config.drift(_digests(roots), _digests(other))


def test_a_different_backup_recipient_is_drift(roots: config.Roots) -> None:
    # Rotating the backup generation has to reach the box, which holds the
    # public halves in its Ignition.
    other = config.Roots(ca=roots.ca, age_recipients=(RECIPIENT, 'age1second'))

    assert config.drift(_digests(roots), _digests(other)) == ['age_recipients']


def test_the_server_key_is_outside_the_bill_of_materials(roots: config.Roots) -> None:
    # Not an oversight: it is random at issuance, so digesting it would be
    # digesting this render rather than this machine. Rotating it is
    # `provision --replace`.
    assert 'server_key' not in _digests(roots)


def test_the_dump_key_secret_is_outside_the_bill_of_materials(roots: config.Roots) -> None:
    # The digest map travels in the instance's metadata. What the converge
    # compares is the key's identity, which is `b2_dump_key_id`.
    assert 'b2_dump_key' not in _digests(roots)


def test_every_field_but_the_two_secrets_is_compared(roots: config.Roots) -> None:
    # Each field declares its own digest treatment, so a field added to the
    # machine is compared unless it says otherwise, and neither a rename nor a
    # new field can quietly drop a component out of the comparison.
    compared = set(_digests(roots)) - {'butane'}

    assert compared == {spec.name for spec in fields(config.Machine)} - {'server_key', 'b2_dump_key'}


def test_the_certificate_the_box_gets_matches_the_key_it_gets(roots: config.Roots) -> None:
    # One issuance, both halves. Two calls would give the box a certificate
    # its private key does not answer for, and 5432 would never come up.
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization

    values = config.machine(roots, address=ADDRESS, dump_key_id='k', dump_key='s', bucket_id='b')
    cert = x509.load_pem_x509_certificate(values.server_cert.encode())
    key = serialization.load_pem_private_key(values.server_key.encode(), password=None)

    assert key.public_key().public_bytes(  # pyright: ignore[reportAttributeAccessIssue]
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    ) == cert.public_key().public_bytes(
        encoding=serialization.Encoding.DER,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


@pytest.fixture
def vault(tmp_path: Path) -> escrow.Vault:
    kit = MemoryKit()
    registry = escrow.Registry.open(tmp_path / 'escrow')
    _ = escrow.init(kit, registry)
    return escrow.Vault.open(kit, registry)


@needs_age
def test_a_bring_up_escrows_the_roots_it_is_about_to_install(vault: escrow.Vault) -> None:
    # The appliance is the first thing to escrow: a bring-up has a kit and an
    # empty registry, and provisioning mints what it needs on the way.
    roots = config.Roots.ensure(vault)

    for label in config.Roots.labels():
        assert vault.registry.generations(label) == [1]
    assert roots.age_recipients == tuple(age.recipient(vault.recover(label)) for label in escrow.backup_labels())


@needs_age
def test_a_second_run_reuses_what_is_already_escrowed(vault: escrow.Vault) -> None:
    # Generating over a live CA would invalidate every certificate under it,
    # and over a live backup identity would orphan every dump.
    first = config.Roots.ensure(vault)

    second = config.Roots.ensure(vault)

    assert second.ca.key_pem == first.ca.key_pem
    assert second.age_recipients == first.age_recipients
    for label in config.Roots.labels():
        assert vault.registry.generations(label) == [1]


@needs_age
def test_writing_a_bundle_does_not_mint_a_ca(vault: escrow.Vault) -> None:
    # `recover` is the read-only door: a machine asking for a client bundle
    # against an empty registry must be told to run the bring-up, not handed a
    # brand-new CA the appliance has never heard of.
    with pytest.raises(escrow.EscrowError, match=escrow.CA):
        _ = config.Roots.recover(vault)
