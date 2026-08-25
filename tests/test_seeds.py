"""The derivation layer is load-bearing for recovery: a wrong byte here means a
pg_dump nobody can open years later, so the primitive is checked against RFC
5869 and the labelled secrets against each other."""

import pytest

from kluster.scripts.credentials import seeds

# RFC 5869 test case 3: SHA-256, zero-length salt and info.
RFC_CASE_3_IKM = bytes.fromhex('0b' * 22)
RFC_CASE_3_OKM = bytes.fromhex('8da4e775a563c18f715f802a063c5a31b8a11f5c5ee1879ec3454e5f3c738d2d9d201395faa4b61a96c8')

# RFC 5869 test case 1: SHA-256, with salt and info.
RFC_CASE_1_IKM = bytes.fromhex('0b' * 22)
RFC_CASE_1_SALT = bytes.fromhex('000102030405060708090a0b0c')
RFC_CASE_1_INFO = bytes.fromhex('f0f1f2f3f4f5f6f7f8f9')
RFC_CASE_1_OKM = bytes.fromhex('3cb25f25faacd57a90434f64d0362f2a2d2d0a90cf1a5a4c5db02d56ecc4c5bf34007208d5b887185865')

SEED = bytes(range(32))


def test_hkdf_matches_rfc5869_case_3() -> None:
    assert seeds.hkdf(RFC_CASE_3_IKM, b'', b'', 42) == RFC_CASE_3_OKM


def test_hkdf_matches_rfc5869_case_1() -> None:
    assert seeds.hkdf(RFC_CASE_1_IKM, RFC_CASE_1_SALT, RFC_CASE_1_INFO, 42) == RFC_CASE_1_OKM


def test_derivation_is_deterministic() -> None:
    assert seeds.derive(SEED, 'a') == seeds.derive(SEED, 'a')


def test_labels_separate_secrets() -> None:
    assert seeds.derive(SEED, 'a') != seeds.derive(SEED, 'b')
    assert seeds.age_seed(SEED, 1) != seeds.age_seed(SEED, 2)
    assert seeds.cert_scalar(SEED, 'ci') != seeds.cert_scalar(SEED, 'operator')


def test_roots_separate_secrets() -> None:
    other = bytes(range(1, 33))
    assert seeds.pulumi_passphrase(SEED) != seeds.pulumi_passphrase(other)


def test_short_root_is_rejected() -> None:
    with pytest.raises(ValueError):
        _ = seeds.derive(b'too short', 'a')


def test_passwords_are_url_safe_and_unpadded() -> None:
    password = seeds.pulumi_passphrase(SEED)
    assert '=' not in password
    assert password.isascii()


def test_age_url_matches_the_pinned_version() -> None:
    """A version bumped without its URL would fetch the old binary and pass
    its own digest check."""
    from kluster.scripts.state_backend import settings

    assert settings.AGE_VERSION in settings.AGE_URL
    assert settings.AGE_URL.endswith('linux-amd64.tar.gz')
    assert len(settings.AGE_SHA256) == 64
