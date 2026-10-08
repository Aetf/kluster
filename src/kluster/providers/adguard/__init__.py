"""An AdGuard Home instance's whole configuration, through its administration API.

AdGuard Home has no Terraform or Pulumi provider, so its configuration is
declared as dynamic resources, one per subsystem of one instance. Each kind
owns one subsystem's endpoints and has one write shape, so a diff or a failure
is scoped to what changed, and no endpoint has two writers:

| Kind | Owns | Write shape |
|---|---|---|
| `AdGuardUserRules` | the custom filtering rules | one request, the whole list |
| `AdGuardDnsServer` | the DNS server's settings, protection, the access lists | one request per endpoint, every declared field |
| `AdGuardFiltering` | filtering on/off and update interval, safe browsing, parental control, safe search, blocked services | whole object per endpoint |
| `AdGuardFilterLists` | the block and allow lists | per-list endpoints, reconciled as a set |
| `AdGuardClients` | the persistent clients | per-client endpoints, whole client, reconciled as a set |
| `AdGuardLogSettings` | the query-log and statistics settings | whole object per endpoint |

Every kind's object is in the API's own shape and names, and the lifecycle they
share is `base`'s: a `diff` that calls no instance, a `read` that reports what
the instance holds as the inputs, an `update` that writes only what differs, and
a `delete` that calls nothing. The six write disjoint endpoints and may run in
parallel: the instance serializes its own configuration writes.

What this package leaves alone, the rest of the API reaches: the rewrite list,
which `AdGuardUserRules` guards and nothing writes; TLS, DHCP, the UI's
presentation, and the accounts, which no endpoint creates.

**A resource is identified by the instance, not by the address it answers on.**
`instance` is the caller's own name for the resolver, and it is what the
resource id is built from; `endpoint` is where this run reaches that instance
and is a declared input like any other. Re-addressing an instance is then an
update that writes nothing. A resource names one instance, so an instance that
is down fails its own resources and leaves the other instance's converged.

**The credential is an admin login, because that is the whole of what the
appliance offers.** AdGuard Home has no scoped API tokens: the account that
signs into the web interface is the account every call authenticates as. It is
read in `configure`, out of stack configuration, inside the plugin's process --
no caller declares it, no component passes it, and nothing pickles it. `check`
stamps every resource with `session` -- the address, and a short digest of the
login -- and `provider_version`, through `kluster.providers.configured`.

What an instance is configured with is the caller's business, not this
package's.
"""

from kluster.providers.adguard.base import PASSWORD_CONFIG, USERNAME_CONFIG
from kluster.providers.adguard.clients import AdGuardClients, Client
from kluster.providers.adguard.dns_server import AccessLists, AdGuardDnsServer, DnsSettings
from kluster.providers.adguard.filter_lists import AdGuardFilterLists, FilterList
from kluster.providers.adguard.filtering import AdGuardFiltering, BlockedServices, FilteringSwitch, SafeSearch, Switch
from kluster.providers.adguard.log_settings import AdGuardLogSettings, QueryLogConfig, StatsConfig
from kluster.providers.adguard.user_rules import AdGuardUserRules, RewriteListNotEmpty

__all__ = (
    'PASSWORD_CONFIG',
    'USERNAME_CONFIG',
    'AccessLists',
    'AdGuardClients',
    'AdGuardDnsServer',
    'AdGuardFilterLists',
    'AdGuardFiltering',
    'AdGuardLogSettings',
    'AdGuardUserRules',
    'BlockedServices',
    'Client',
    'DnsSettings',
    'FilterList',
    'FilteringSwitch',
    'QueryLogConfig',
    'RewriteListNotEmpty',
    'SafeSearch',
    'StatsConfig',
    'Switch',
)
