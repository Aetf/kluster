"""An AdGuard Home instance's whole configuration, through its administration API.

AdGuard Home has no Terraform or Pulumi provider, so its configuration is
declared as dynamic resources, one per subsystem of one instance. Each kind
owns one subsystem's endpoints and has one write shape, so a diff or a failure
is scoped to what changed, and no endpoint has two writers:

| Kind | Owns | Write shape |
|---|---|---|
| `AdGuardSetup` | the account and the listen addresses, which only first run sets | one request, in first run only |
| `AdGuardUserRules` | the custom filtering rules, and the rewrite list's switch | one request each, the whole list |
| `AdGuardDnsServer` | the DNS server's settings, protection, the access lists | one request per endpoint, every declared field |
| `AdGuardFiltering` | filtering on/off and update interval, safe browsing, parental control, safe search, blocked services | whole object per endpoint |
| `AdGuardFilterLists` | the block and allow lists | per-list endpoints, reconciled as a set |
| `AdGuardClients` | the persistent clients | per-client endpoints, whole client, reconciled as a set |
| `AdGuardLogSettings` | the query-log and statistics settings | whole object per endpoint |

Every kind's object is in the API's own shape and names. Its inputs are typed
with this package's `TypedDict`s and its outputs as plain dicts and lists of
them, because the SDK holds an output resolved in the program to the
annotation on the resource class and refuses a dict where that names a
`TypedDict`. The lifecycle the kinds share is `base`'s: a `diff` that calls no
instance, a `read` that reports what the instance holds as the inputs, an
`update` that writes only what differs, and a `delete` that calls nothing. The
six below `AdGuardSetup` in the table write disjoint endpoints and may run in
parallel: the instance serializes its own configuration writes.

**Each of the six depends on its own instance's `AdGuardSetup`**, declared by
the caller, because an instance that starts with no configuration file is in
first run: it answers no DNS and holds no account, and only the setup's
`configure` ends that. Every kind classifies its instance before it sends the
login (`api.classify`), once per instance per `pulumi` command. A kind's `read`
of an instance in first run reports the resource gone, so a refreshing run
drops all seven of that instance's resources and re-creates them, setup first;
an instance that is down, refuses the login or holds no account raises, and
keeps its state.

What this package leaves alone, the rest of the API reaches: the rewrite list's
rows, which `AdGuardUserRules` guards and nothing writes; TLS, DHCP, the UI's
presentation, and any account but the one the setup makes, since no endpoint
lists, changes or deletes one.

**A resource is identified by the instance, not by the address it answers on.**
`instance` is the caller's own name for the resolver, and it is what the
resource id is built from; `endpoint` is where this run reaches that instance's
API, and `setup_endpoint` its setup wizard, each a declared input like any
other. Re-addressing an instance is then an update that writes nothing. A
resource names one instance, so an instance that is down fails its own
resources and leaves the other instance's converged.

**The credential is an admin login, because that is the whole of what the
appliance offers.** AdGuard Home has no scoped API tokens: the account that
signs into the web interface is the account every call authenticates as. It is
read in `configure`, out of stack configuration, inside the plugin's process --
no caller declares it, no component passes it, and nothing pickles it. `check`
stamps every resource with `session` -- the address, and a short digest of the
login -- and `provider_version`, through `kluster.providers.configured`. The
setup gives an instance in first run that login as its one account, so the
login is drawn by the operator, and a rotation is a reset of each instance.

What an instance is configured with is the caller's business, not this
package's, apart from the rewrite list, which every `AdGuardUserRules` holds
empty and switched off.
"""

from kluster.providers.adguard.base import PASSWORD_CONFIG, USERNAME_CONFIG
from kluster.providers.adguard.clients import AdGuardClients, Client
from kluster.providers.adguard.dns_server import AccessLists, AdGuardDnsServer, DnsSettings
from kluster.providers.adguard.filter_lists import AdGuardFilterLists, FilterList
from kluster.providers.adguard.filtering import AdGuardFiltering, BlockedServices, FilteringSwitch, SafeSearch, Switch
from kluster.providers.adguard.log_settings import AdGuardLogSettings, QueryLogConfig, StatsConfig
from kluster.providers.adguard.setup import Address, AdGuardSetup, Listen
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
    'AdGuardSetup',
    'AdGuardUserRules',
    'Address',
    'BlockedServices',
    'Client',
    'DnsSettings',
    'FilterList',
    'FilteringSwitch',
    'Listen',
    'QueryLogConfig',
    'RewriteListNotEmpty',
    'SafeSearch',
    'StatsConfig',
    'Switch',
)
