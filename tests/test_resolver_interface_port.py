"""The one port every declaration of the resolvers' interface names.

Four declarations reach that interface, spread over two components and three
renderers: the proxy's vhost for each of the two instances, the initial state
that tells an instance where to listen, and the overlay flow rule that admits a
continuous-integration member to it. Each is derived from one convention today,
and this module is what notices when one stops being — a site changed on its
own leaves the other three pointed at a port nothing listens on, and the
symptom is an interface that answers nobody rather than a diff that looks
wrong.

The port is written literally because the value is the appliance's rather than
this program's: both instances bind their interface to 80, and the declaration
follows them (`conventions.gateway.ADGUARD_API_PORT`). A case reading the
constant on both sides of every assertion would hold the four together but
agree with whatever the constant said, including a value no instance answers
on.
"""

from __future__ import annotations

from test_device_services import caddy
from test_flow_rules import RESOLVERS, rules

from kluster import conventions
from kluster.components.gateway import container

#: Where the appliance answers, and therefore what every site must name.
PORT = 80


def test_every_declaration_of_the_resolvers_interface_names_the_same_port() -> None:
    """The two vhosts, the initial state and the flow rule, held to one value."""
    # The proxy dials the same two addresses again in its legacy block, for the
    # three names the device answers today, so the file is cut at that block:
    # what the two vhosts are is the part serving the device's own zone, and an
    # assertion satisfied by a legacy row would not notice a vhost moving.
    served, _, _ = container.caddyfile(caddy()).partition(f'*.{conventions.gateway.ZONE_LEGACY}')
    for resolver in conventions.gateway.RESOLVERS:
        assert f'reverse_proxy http://{resolver.address}:{PORT}\n' in served

    address = conventions.gateway.ADGUARD_ALICE.address
    assert f'address: {address}:{PORT}\n' in container.adguard_initial_state(address)

    # The rule program is a pure function of the addresses it is handed, so the
    # ones here are that module's literals rather than the census's; what this
    # case is about is the port beside them.
    rendered = rules()
    for site_address in RESOLVERS:
        assert f'ipdest {site_address}/32 and dport {PORT};' in rendered
