"""The OCI seed drill: rotate, then rotate again, against the real tenancy.

The offline tests drive a fake tenancy, which can only ever contain what we
already know Oracle does. This drill exercises the one cycle the bring-up
proved dangerous against the service itself: a rotation, and then a rotation
from the state the first one left. Both of the defects that offline tests
could not have caught -- a freshly uploaded key that does not authenticate
yet, and an identity-domains tenancy that refuses a deletion -- are on this
path, and so is the three-key quota that a rotation has to make room in
before it mints.

The invariant asserted after each rotation is the one the operator depends on:
**exactly one usable key stands, barring deletions the service refuses**. The
key the kit holds is proved usable by being the key that lists the user's keys;
a survivor is proved refused by asking for its deletion and being told no. A
survivor that deletes on request is a sweep that did not run, which is a
regression rather than a tenancy quirk, and fails the drill.

Safe to repeat: rotation is the seed's ordinary maintenance operation, the
tenancy is a free one, and every run ends where it started -- one key on the
seed user, its private half in the kit.
"""

# The SDK ships no stubs; the same waiver `oci_iam.py` itself carries.
# pyright: reportMissingTypeStubs=false, reportUnknownMemberType=false
# pyright: reportUnknownArgumentType=false

from __future__ import annotations

import logging

import oci
import pytest

from kluster.scripts.credentials import entries, oci_iam
from kluster.scripts.credentials.kdbx import KdbxStore

log = logging.getLogger(__name__)

#: The register row this drill rotates (docs/credentials.md §2).
SEED_ENTRY = entries.SEEDS['oci'].entry

#: How many rotations one run performs. Two, because the second is the one
#: that starts from a state a rotation produced rather than from a bring-up.
ROUNDS = 2


@pytest.fixture(scope='module')
def kit() -> KdbxStore:
    """The kit, opened exactly the way the `credentials` command opens it.

    `from_env` reads `$KLUSTER_KDBX`, and `unlock` takes the master password
    from the desktop secret store, falling back to a prompt -- so a drill on a
    machine where `credentials kdbx remember` has run needs no input, and one
    where it has not needs `pytest -s` to be able to ask.
    """
    store = KdbxStore.from_env()
    store.unlock()
    return store


def _authorized(kit: KdbxStore) -> tuple[oci_iam.Iam, str, str]:
    """Connect to the tenancy as the key the kit currently holds.

    Returns the client, the seed's user OCID and the stored key's fingerprint.
    Building this at all is half the assertion: a kit whose key the tenancy
    will not accept cannot get past `authorize`.
    """
    tenancy, user_id, private_pem = oci_iam.load_seed(kit, SEED_ENTRY)
    iam = oci_iam.Iam.authorize(tenancy, user_id, private_pem)
    return iam, user_id, oci_iam.fingerprint(private_pem)


def _one_usable_key(iam: oci_iam.Iam, user_id: str, current: str) -> list[str]:
    """Assert the invariant and return the fingerprints the service refused to drop.

    `iam` must be authorized as `current`: an identity-domains tenancy lets a
    user manage its own credentials and refuses the account root the
    equivalent call, which is also why this call proves the stored key works.
    """
    held = iam.api_keys(user_id)
    assert current in held, f'the kit holds {current}, which the tenancy does not list among {held}'

    refused: list[str] = []
    deletable: list[str] = []
    for extra in held:
        if extra == current:
            continue
        try:
            iam.delete_api_key(user_id, extra)
        except oci.exceptions.ServiceError as exc:
            log.warning('the tenancy refuses to delete %s (%s); it stays as a console errand', extra, exc.code)
            refused.append(extra)
        else:
            deletable.append(extra)

    assert not deletable, f'rotation left deletable keys behind, so the sweep did not run: {deletable}'
    return refused


def test_rotating_twice_leaves_exactly_one_usable_key(kit: KdbxStore) -> None:
    if not kit.has(SEED_ENTRY):
        pytest.skip(f'no {SEED_ENTRY} row in this kit; the drill rotates a seed, it does not create one')

    _, user_id, initial = _authorized(kit)
    log.info('drill starting on %s, holding %s', user_id, initial)
    seen = [initial]
    refused: list[str] = []

    for round_number in range(1, ROUNDS + 1):
        current = oci_iam.rotate_seed(kit, seed_entry=SEED_ENTRY)
        assert current not in seen, f'round {round_number} re-minted a fingerprint already seen: {current}'

        iam, rotated_user, stored = _authorized(kit)
        # What rotates is the key material; the user, group and policy under
        # it are the same objects afterwards.
        assert rotated_user == user_id
        assert stored == current, f'round {round_number} stored {stored} but minted {current}'

        refused += _one_usable_key(iam, user_id, current)
        seen.append(current)
        log.info('round %d: %s stands', round_number, current)

    log.info('drill finished on %s, holding %s; refused deletions: %s', user_id, seen[-1], refused or 'none')
