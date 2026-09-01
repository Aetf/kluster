"""What the `dns` stack takes as configuration, and what it refuses to.

The stack's configuration is three secrets: the zones token the Cloudflare
provider is built from, and the AdGuard login the rewrite provider opens the
instances with. Everything else the program needs is a decision, so it is code
-- the account the zones belong to among it, which is the side of rfc-002
§10.3's split that account facts fall on.

Two properties are worth a suite of their own, because both are about a
configuration the program does *not* get: that the zones token is read from
this project's namespace and not from the provider's, and that a run whose
configuration names no account still declares every zone.
"""

from typing import Any

import pulumi
import pytest
from mock_monitor import Recorder, declaring, run_with

from kluster import conventions
from kluster.stacks import dns

ZONE = 'cloudflare:index/zone:Zone'

API_TOKEN = 'a-zones-token'

#: What the committed file spells the zones token as: the program's own
#: unqualified key, which `pulumi.Config()` resolves against the project name.
API_TOKEN_KEY = f'kluster:{dns.CLOUDFLARE_API_TOKEN}'

#: The namespace the provider would also accept its token from, and the one
#: this stack no longer has: with default providers disabled for `cloudflare`
#: there is nothing left for it to configure.
PROVIDER_NAMESPACE_KEY = 'cloudflare:apiToken'


class NoPhysical(Recorder):
    """A `physical` whose stack exists and publishes nothing.

    The anchors are unknown under it, which is what a preview before the first
    `up` of `physical` looks like -- and irrelevant to every case here, which
    is why this suite takes the cheapest monitor that lets the program run.
    """

    def computed(self, args: pulumi.runtime.MockResourceArgs) -> dict[str, Any]:
        if args.typ == 'pulumi:pulumi:StackReference':
            return {'outputs': {}}
        return {}


async def _declared(config: dict[str, str]) -> NoPhysical:
    """The whole program against `config`, and the monitor that watched it."""
    pulumi.runtime.set_all_config(config)
    monitor = await run_with(NoPhysical(), stack='dns', preview=True)
    async with declaring():
        await dns.main()
    return monitor


@pytest.mark.asyncio
async def test_the_zones_token_is_read_from_this_project_s_own_namespace() -> None:
    """A value in the provider's namespace is not a value this stack reads.

    The configuration here is the one a stale stack file would leave behind:
    the token where the provider itself would take it, and nothing under the
    project's name. A program that picked it up from there would be reading
    ambient configuration, which is what disabling the default provider exists
    to prevent -- so the run has to stop by naming the key it wants.
    """
    pulumi.runtime.set_all_config({PROVIDER_NAMESPACE_KEY: API_TOKEN})
    _ = await run_with(NoPhysical(), stack='dns', preview=True)

    with pytest.raises(pulumi.ConfigMissingError, match=API_TOKEN_KEY):
        await dns.main()


@pytest.mark.asyncio
async def test_the_zones_token_is_the_only_key_the_program_reads() -> None:
    """It is the whole of the configuration a `dns` declaration needs.

    The AdGuard login is in the stack file too, but the rewrite provider reads
    it in its own `configure` (rfc-002 §7.4), so a program handed the token
    alone declares everything this stack has.
    """
    monitor = await _declared({API_TOKEN_KEY: API_TOKEN})

    assert set(monitor.by_name(ZONE)) == set(conventions.ALL_ZONES)


@pytest.mark.asyncio
async def test_the_account_the_zones_are_created_in_is_a_convention() -> None:
    """No key names the account, and every zone carries it all the same.

    An account identifier names an account rather than authenticating to it,
    so it has one home in code beside the cloud tenancy and the backup
    account's region. A stack that read it from configuration would take a
    fact through the channel its secrets travel on, and an operator filling a
    new stack in would have one more value to find.
    """
    monitor = await _declared({API_TOKEN_KEY: API_TOKEN})

    account = {'id': conventions.CLOUDFLARE_ACCOUNT.account_id}
    declared = monitor.by_name(ZONE)
    assert declared
    assert all(inputs['account'] == account for inputs in declared.values())
