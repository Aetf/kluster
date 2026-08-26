"""The derived credentials (docs/credentials.md §3): minted from a seed, pushed to a slot.

One function per register row, each of them mint -> push -> verify inside a
single run, and therefore idempotent: rotating a row is re-running it. Nothing
here ever writes to the kit — a derived credential in the offline store would
be the staging area §1 rule 2 forbids.

A row appears here when its consumer exists. The remaining Cloudflare rows —
the DNS-01 token for cert-manager and the gateway's ACME token — have nowhere
to be delivered yet, and a mint with no slot would be exactly the parked secret
the register rules out.
"""

from __future__ import annotations

import logging

from ... import conventions
from . import cloudflare, entries, pulumi_config
from .kdbx import KdbxStore

log = logging.getLogger(__name__)

#: The name the zones token is minted under. Stable, because it is the name
#: retirement matches on: a re-run deletes the same-named predecessor once its
#: successor is verified, so one live token of this name is the invariant.
ZONES_TOKEN_NAME = 'kluster-zones'

#: The stack that manages the estate's DNS records, and therefore the slot the
#: zones token is delivered into.
ZONES_STACK = 'dns'

#: Where the Cloudflare provider reads its credential, and where the program
#: reads the account that owns the zones. The provider's key is a secret; the
#: account id is an identifier the committed file may carry in plain text.
API_TOKEN_KEY = 'cloudflare:apiToken'
ACCOUNT_KEY = 'kluster:cloudflareAccountId'

SEED_ENTRY = entries.SEEDS['cloudflare'].entry


def cloudflare_zones(kit: KdbxStore, *, stack: pulumi_config.Stack, seed_entry: str = SEED_ENTRY) -> str:
    """Mint the zones token from the seed and install it in a stack's config.

    The scope is the estate's zones as `conventions` lists them, so adding a
    zone there and re-running is the whole procedure for widening it. Returns
    the account id it discovered, which the same push writes beside the token.
    """
    zones = conventions.ALL_ZONES
    log.info('opening the Cloudflare seed from the kit')
    session = cloudflare.Session.from_entry(kit, seed_entry)
    minted = cloudflare.mint_zone_token(session, name=ZONES_TOKEN_NAME, zones=zones)

    stack.ensure()
    stack.set_secret(API_TOKEN_KEY, minted.value)
    stack.set(ACCOUNT_KEY, minted.account_id)
    log.info(
        'the %s stack holds a token scoped to %s; commit Pulumi.%s.yaml to publish the slot',
        stack.name,
        ', '.join(zones),
        stack.name,
    )
    return minted.account_id
