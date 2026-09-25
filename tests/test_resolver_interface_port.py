"""The one port every declaration of the resolvers' interface names.

The declarations that reach that interface — the proxy's vhost for each
instance, the initial state that tells an instance where to listen, the overlay
flow rule that admits a continuous-integration member to it, and the address
the `dns` stack writes rewrites to. Each is derived from one convention today,
and this module is what notices when one stops being — a site changed on its
own leaves the rest pointed at a port nothing listens on, and the symptom is an
interface that answers nobody rather than a diff that looks wrong.

The port is named through `conventions.gateway.ADGUARD_API_PORT` on both sides
of every assertion. That the value is the appliance's rather than this
program's is a constraint on that declaration, and it is stated there, where
whoever edits it is looking; a copy of it here would redden for nobody but that
editor, and would pass a wrong new value as readily as a right one. What the
constant cannot state, and this module does, is that the sites above each reach
the port on their own and still reach the same one -- every production side
here is a renderer or an accessor, free to move with no new value typed
anywhere.
"""

from __future__ import annotations

from gateway_services import caddy, served
from overlay_flow_rules import RESOLVERS, rules

from kluster import conventions
from kluster.components.gateway import container


def test_every_declaration_of_the_resolvers_interface_names_the_port_the_appliance_answers_on() -> None:
    """The two vhosts, the initial state, the flow rule and the rewrite endpoint."""
    port = conventions.gateway.ADGUARD_API_PORT

    vhosts = served(container.caddyfile(caddy()))
    for resolver in conventions.gateway.RESOLVERS:
        assert resolver.vhost is not None
        assert vhosts[resolver.vhost] == (f'reverse_proxy http://{resolver.address}:{port}',)

    address = conventions.gateway.ADGUARD_ALICE.address
    assert f'address: {address}:{port}\n' in container.adguard_initial_state(address)

    # The rule program is a pure function of the addresses it is handed, so the
    # ones here are that module's literals rather than the census's; what this
    # case is about is the port beside them.
    rendered = rules()
    for site_address in RESOLVERS:
        assert f'ipdest {site_address}/32 and dport {port};' in rendered

    # Where the `dns` stack writes a rewrite. Assembled here rather than
    # compared against the accessor's own output, which would agree with any
    # definition it was given: the scheme, the address and the port are each a
    # separate way for the endpoint to name something the flow rule does not
    # admit or the appliance does not answer, and none of the three fails
    # anywhere but a live apply.
    for resolver in conventions.gateway.RESOLVERS:
        assert conventions.gateway.resolver_api_url(resolver) == f'http://{resolver.address}:{port}'
