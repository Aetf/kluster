"""One resolver's whole configuration, and the component that declares it on that instance.

`AdGuardConfiguration` is everything an instance is configured with, in the
administration API's own shapes (`kluster.providers.adguard`); the program
builds one (`resolver_settings.configuration`) and hands the same one to the
component of each instance, since the two do not differ in any setting.

`ResolverConfiguration` declares it on one instance: the first-run setup, and
one resource of each kind that configures a running instance, each depending
on that setup. One component per instance rather than one over the pair:
their independence is the design (dns.md §3), and as two sibling components
that is what the resource tree says. An instance that is down fails its own
resources, and the other's converge wherever they had already started: once
a step fails the engine starts no further one, so a kind still waiting on a
setup that is being created or updated does not run. In the steady state no
setup changes and every kind's step starts.

**No two resolvers restart their DNS listeners at once**, the site's rule
for the set of them (`components.gateway.container`), since the LAN keeps
resolving through whichever one is up. A write of the DNS server's
settings restarts the instance's listener (`AdGuardDnsServer`): the
instance's `dns_config` handler is the one that reconfigures the server,
and no other kind's endpoint does. So the component's `restarting`
resources are actuated behind `after`, the restarting resources of the
instance before it, which the program hands in following the order of
`conventions.gateway.RESOLVERS`. The order holds on every update, not only
on the first create; its cost is that a restarting write held behind an
instance that failed waits for the next run. Dual-writing is also why no
synchronizer runs between the instances: one would overwrite whichever
instance the stack wrote second.

The instance is taken as its census entry rather than as a URL, which leaves
`conventions.gateway.resolver_api_url` and `resolver_setup_url` the only
spellings of where it answers. The configuration is taken as a parameter for
the opposite reason: deriving it in here would be a component reaching for the
censuses it is derived from. The instances' login is not a parameter either:
it is the provider's own, read in `configure` out of the stack's
configuration, so nothing on this side of the boundary holds it.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import pulumi

from kluster import conventions
from kluster.providers.adguard import (
    AccessLists,
    AdGuardClients,
    AdGuardDnsServer,
    AdGuardFiltering,
    AdGuardFilterLists,
    AdGuardLogSettings,
    AdGuardSetup,
    AdGuardUserRules,
    BlockedServices,
    Client,
    DnsSettings,
    FilteringSwitch,
    FilterList,
    Listen,
    QueryLogConfig,
    SafeSearch,
    StatsConfig,
    Switch,
)
from putils import Component

__all__ = ('AdGuardConfiguration', 'ResolverConfiguration')


@dataclass(frozen=True)
class AdGuardConfiguration:
    """Everything one instance is configured with, each part in the shape the kind that writes it takes."""

    #: Where the first-run setup binds the web server and the DNS server.
    listen: Listen
    #: The whole custom-rule list, rendered (`rewrites.user_rules`).
    rules: tuple[str, ...]
    dns: DnsSettings
    access: AccessLists
    filtering: FilteringSwitch
    safebrowsing: Switch
    parental: Switch
    safe_search: SafeSearch
    blocked_services: BlockedServices
    #: The block lists.
    filters: tuple[FilterList, ...]
    #: The allow lists.
    whitelist_filters: tuple[FilterList, ...]
    #: The persistent clients, drawn from the blocklist rows (`rewrites.blocklist_clients`).
    clients: tuple[Client, ...]
    querylog: QueryLogConfig
    stats: StatsConfig


class ResolverConfiguration(Component, pulumi_type='kluster:dns:ResolverConfiguration'):
    """One instance's whole configuration, written to it directly: its setup, then the kinds that configure it running."""

    def __init__(
        self,
        name: str,
        *,
        resolver: conventions.gateway.BridgedService,
        configuration: AdGuardConfiguration,
        after: Sequence[pulumi.Resource] = (),
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(name, opts=opts)

        # The instance is named by its census entry and addressed separately:
        # the name is what identifies a resource, and the addresses are where
        # this run happens to reach the instance. Moving the instance is then
        # an update that writes nothing rather than a replacement.
        instance = resolver.name
        endpoint = conventions.gateway.resolver_api_url(resolver)
        setup_endpoint = conventions.gateway.resolver_setup_url(resolver)

        self.setup: AdGuardSetup = AdGuardSetup(
            f'{name}-setup',
            instance=instance,
            endpoint=endpoint,
            setup_endpoint=setup_endpoint,
            listen=configuration.listen,
            opts=self.child_opts(),
        )

        # An instance that started with no configuration file holds no
        # account and answers no DNS until its setup has run, so every kind
        # that configures a running instance waits for it.
        self.user_rules: AdGuardUserRules = AdGuardUserRules(
            f'{name}-user-rules',
            instance=instance,
            endpoint=endpoint,
            setup_endpoint=setup_endpoint,
            rules=list(configuration.rules),
            opts=self.child_opts(depends_on=[self.setup]),
        )
        self.dns_server: AdGuardDnsServer = AdGuardDnsServer(
            f'{name}-dns-server',
            instance=instance,
            endpoint=endpoint,
            setup_endpoint=setup_endpoint,
            dns=configuration.dns,
            access=configuration.access,
            # Its write restarts the instance's listener, so it waits for the
            # restarting writes of the instance before it (`after`).
            opts=self.child_opts(depends_on=[self.setup, *after]),
        )
        self.filtering: AdGuardFiltering = AdGuardFiltering(
            f'{name}-filtering',
            instance=instance,
            endpoint=endpoint,
            setup_endpoint=setup_endpoint,
            filtering=configuration.filtering,
            safebrowsing=configuration.safebrowsing,
            parental=configuration.parental,
            safe_search=configuration.safe_search,
            blocked_services=configuration.blocked_services,
            opts=self.child_opts(depends_on=[self.setup]),
        )
        self.filter_lists: AdGuardFilterLists = AdGuardFilterLists(
            f'{name}-filter-lists',
            instance=instance,
            endpoint=endpoint,
            setup_endpoint=setup_endpoint,
            filters=list(configuration.filters),
            whitelist_filters=list(configuration.whitelist_filters),
            opts=self.child_opts(depends_on=[self.setup]),
        )
        self.clients: AdGuardClients = AdGuardClients(
            f'{name}-clients',
            instance=instance,
            endpoint=endpoint,
            setup_endpoint=setup_endpoint,
            clients=list(configuration.clients),
            opts=self.child_opts(depends_on=[self.setup]),
        )
        self.log_settings: AdGuardLogSettings = AdGuardLogSettings(
            f'{name}-log-settings',
            instance=instance,
            endpoint=endpoint,
            setup_endpoint=setup_endpoint,
            querylog=configuration.querylog,
            stats=configuration.stats,
            opts=self.child_opts(depends_on=[self.setup]),
        )

        #: The resources whose write restarts the instance's DNS listener: what
        #: the next instance's own restarting writes are actuated behind.
        self.restarting: tuple[pulumi.Resource, ...] = (self.dns_server,)

        self.register_outputs({})
