"""The derived age identity is checked against age itself: a Bech32 or clamping
mistake would produce a key that looks fine and decrypts nothing."""

import shutil
import subprocess as sp

import pytest

from kluster.scripts.credentials import age, seeds

SEED = bytes(range(32))

age_keygen = shutil.which('age-keygen')
needs_age = pytest.mark.skipif(age_keygen is None, reason='age-keygen not on PATH (mise x -- ...)')


def test_bech32_matches_bip173_vector() -> None:
    # BIP-173's valid-checksum list: empty data under hrp 'a'. Segwit
    # addresses are not a vector for this function -- they prepend a
    # witness version to the data part.
    assert age.bech32_encode('a', b'') == 'a12uel5l'


@needs_age
def test_derived_public_key_matches_age_keygen() -> None:
    identity = age.generation(SEED, 1)
    assert age_keygen is not None
    proc = sp.run(
        [age_keygen, '-y'],
        input=f'{identity.secret}\n',
        capture_output=True,
        text=True,
        check=True,
        timeout=30,
    )
    assert proc.stdout.strip() == identity.public


def test_identity_shape() -> None:
    identity = age.generation(SEED, 1)
    assert identity.secret.startswith('AGE-SECRET-KEY-1')
    assert identity.public.startswith('age1')


def test_generations_differ() -> None:
    assert age.generation(SEED, 1).public != age.generation(SEED, 2).public


def test_scalar_length_is_checked() -> None:
    with pytest.raises(ValueError):
        _ = age.identity_from_scalar(seeds.derive(SEED, 'x', 16))
