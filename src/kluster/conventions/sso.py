"""The single sign-on census: every OpenID Connect client the portal registers.

Authelia is the installation's OpenID Connect provider, and each application
that signs in through it is one of its clients. A client is a contract between
two components: the portal's, which registers it, and the application's, which
asks for it. What both have to agree on is a row of `CLIENTS` -- where the
portal may send a browser back to, who may sign in and with how many factors,
how the application proves it is the client, what it asks for and where it
reads what it gets. Two programs read the census, which is what places it
here: `apps`, which renders Authelia's client list and gives each application
its client, and `k8s-base`, whose monitoring component gives Grafana its
client (declarative/workloads.md §4).

**The census carries every client the legacy authentication service carries,
from Wave A1.** At A1's cut the new service takes the portal's name, and every
legacy application signs in there from then on, so an application that has not
moved has its client already, under the host it keeps across its move. The
census tests hold every client's host to a name that a route row publishes or
that the `dns` stack points at the legacy VPS, in the zone the client names.
Where the gateway's proxy still serves the application under the retiring LAN
zone, its redirect paths are registered under that name too, which
`redirect_uris` derives from `conventions.gateway.LEGACY_VHOSTS`: the name
leaves the client's registration with the vhost's row, and nothing here is
edited for it.

**A client's identifier and secret are not here.** Every client is
confidential, and both halves are sealed in the configuration of the stack that
declares its consumer: the legacy ciphertext as it is where the consumer keeps
its legacy namespace, sealed afresh where it does not, as Grafana's are
(declarative/workloads.md §5).

**Who may sign in is a list of groups, each with its factors, and nothing
else.** A grant names a group and no network: the address the portal sees a
browser at is the browser's own word (declarative/cluster-infra.md §2), so a
rule keyed on it would be one the browser chooses to meet. A subject in none of
a client's groups, a subject with no groups included, is refused: the portal
renders every policy with `default_policy: deny`, because Authelia's own
default is `two_factor`, and no grant names everyone.

**A client reaches the issuer inside the cluster at its home address**
(`Issuer`, `issuer`). The issuer a client validates is the portal's public
name, so its server calls the portal by that name. The cluster's DNS passes a
public name on to the public resolvers, Talos' defaults on the worker, which a
pod at home reaches only through the uplink, as it does the proxy's edge their
answer names, since the portal's row is proxied. The portal's row therefore carries a home side on `lan`, which the
`dns` stack rewrites the name to at home (declarative/dns.md §3), and the
issuer carries the same address for the client's pod to dial the name at: a
host alias every client's serving pod carries
(`kluster.components.apps.issuer_aliases`). Every other call inside the
cluster goes to a Service by its name in the cluster.

Authelia's access-control rules for the applications behind the gate are its
own configuration and stay in its component: they are keyed by route, not by
client.

Read qualified -- `conventions.sso.CLIENTS`, `conventions.sso.Client` --
because the names are common nouns that mean this one thing only while the
portal stands beside them.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from ipaddress import IPv4Address, IPv6Address, ip_address
from typing import final
from urllib.parse import urlsplit

from kluster.conventions.dns import ZONE_PRIMARY
from kluster.conventions.gateway import LEGACY_VHOSTS, LegacyVhost
from kluster.conventions.routes import Exposure, Held, Proxied, Route
from kluster.conventions.site import LAN_POOL

__all__ = (
    'CLIENTS',
    'Claim',
    'Client',
    'Factor',
    'Grant',
    'Issuer',
    'Scope',
    'TokenEndpointAuth',
    'issuer',
    'portal_row',
    'redirect_uris',
)


@final
class Factor(StrEnum):
    """How many factors a grant asks of a subject, as Authelia's policies name them."""

    ONE = 'one_factor'
    TWO = 'two_factor'


@dataclass(frozen=True)
class Grant:
    """A group whose members may sign in to a client, with the factors it asks of them."""

    group: str
    factor: Factor

    def __post_init__(self) -> None:
        if not self.group:
            raise ValueError('a grant names no group, so it would admit every subject the portal knows')


@final
class TokenEndpointAuth(StrEnum):
    """How the application proves at the token endpoint that it is the client, as Authelia's `token_endpoint_auth_method` names it."""

    #: The identifier and secret in the `Authorization` header.
    BASIC = 'client_secret_basic'
    #: The identifier and secret in the form body.
    POST = 'client_secret_post'


@final
class Scope(StrEnum):
    """A scope the application asks for, and the portal grants."""

    OPENID = 'openid'
    PROFILE = 'profile'
    EMAIL = 'email'
    GROUPS = 'groups'
    #: A refresh token, which the portal issues only to a client that may use
    #: the refresh grant: the scope implies the grant.
    OFFLINE_ACCESS = 'offline_access'


@final
class Claim(StrEnum):
    """A claim the portal puts in the ID token, which it does only where the scope that grants it was granted."""

    EMAIL = 'email'
    EMAIL_VERIFIED = 'email_verified'
    NAME = 'name'
    PREFERRED_USERNAME = 'preferred_username'

    @property
    def scope(self) -> Scope:
        """The scope that grants the claim (OpenID Connect Core 1.0 §5.4)."""
        match self:
            case Claim.EMAIL | Claim.EMAIL_VERIFIED:
                return Scope.EMAIL
            case Claim.NAME | Claim.PREFERRED_USERNAME:
                return Scope.PROFILE


#: The schemes a redirect address at a host takes; a native redirect takes a
#: scheme of the application's own instead.
_WEB_SCHEMES = ('http', 'https')


@dataclass(frozen=True)
class Client:
    """One application's client, named after the application.

    The portal names the client's authorization policy, and its ID-token
    claims where it has any, after `name` too.
    """

    name: str
    host: str
    """The label the web redirect addresses are under, in `zone`: the name the
    application keeps across its move."""
    paths: tuple[str, ...]
    """The web redirect addresses' paths under `host`, each from its leading `/`."""
    policy: tuple[Grant, ...]
    """Who may sign in, matched in order: the first grant whose group holds
    the subject applies, and a subject no grant holds is refused."""
    token_endpoint_auth: TokenEndpointAuth
    scopes: tuple[Scope, ...]
    zone: str = ZONE_PRIMARY
    """One zone: an application that signs in through the portal is served in
    one zone, since a second public name for it is a sign-in the portal
    refuses (declarative/dns.md §2). The one other name a client is
    registered under is the legacy LAN name the gateway's proxy still serves
    it at, which `redirect_uris` derives."""
    pkce: bool = False
    """Whether the portal requires a PKCE challenge, by S256, which the
    application then sends."""
    id_token: tuple[Claim, ...] = ()
    """The claims the portal puts in the ID token beside the standard ones,
    for an application that reads its claims there and never asks the
    userinfo endpoint."""
    native: tuple[str, ...] = ()
    """Redirect addresses under a scheme of the application's own, which its
    native app answers on the device rather than any host."""

    def __post_init__(self) -> None:
        what = f'the client {self.name!r}'
        if not self.name:
            raise ValueError('a client has no name, and its policy and claims are named after it')
        if not self.host:
            raise ValueError(f'{what} names no host for its redirect addresses')
        if not self.paths:
            raise ValueError(f'{what} has no redirect address under its host')
        if bad := [path for path in self.paths if not path.startswith('/')]:
            raise ValueError(f'{what} has a redirect path that does not start at the root: {bad}')
        if not self.policy:
            raise ValueError(f'{what} grants no group, so nobody can sign in')
        groups = [grant.group for grant in self.policy]
        if len(groups) != len(set(groups)):
            raise ValueError(f'{what} grants a group twice, and only the first grant ever applies: {groups}')
        if Scope.OPENID not in self.scopes:
            raise ValueError(f'{what} does not ask for `openid`, which is what makes a request OpenID Connect')
        if ungranted := [claim for claim in self.id_token if claim.scope not in self.scopes]:
            raise ValueError(f'{what} reads claims from the ID token under no scope it asks for: {ungranted}')
        if web := [uri for uri in self.native if urlsplit(uri).scheme in (*_WEB_SCHEMES, '')]:
            raise ValueError(f"{what} has a native redirect under a web scheme or none, which is a host's: {web}")


def redirect_uris(client: Client, vhosts: Iterable[LegacyVhost] | None = None) -> tuple[str, ...]:
    """Every redirect address the portal registers for `client`: its paths under each name it is served at, then its native ones.

    The names are its host in its zone, then the legacy LAN name of each row
    of `vhosts` under the same label. `vhosts` is the gateway's census
    unless given.
    """
    legacy = LEGACY_VHOSTS if vhosts is None else vhosts
    hosts = [f'{client.host}.{client.zone}', *(vhost.host for vhost in legacy if vhost.label == client.host)]
    return (*(f'https://{host}{path}' for host in hosts for path in client.paths), *client.native)


@dataclass(frozen=True)
class Issuer:
    """The portal as a client's server reaches it: the issuer it validates, and the addresses its pod dials the issuer's name at."""

    url: str
    """The issuer's identifier, which the client discovers the portal's
    endpoints under."""
    addresses: tuple[IPv4Address | IPv6Address, ...] = ()
    """Where a pod in the cluster reaches the issuer's name, at most one
    address per family; none where the name resolves for a pod as it does
    anywhere else."""

    def __post_init__(self) -> None:
        if not self.host:
            raise ValueError(f'the issuer {self.url!r} names no host')
        if self.addresses and _is_address(self.host):
            raise ValueError(f'the issuer {self.url!r} is an address, so there is no name for a pod to dial elsewhere')
        families = [address.version for address in self.addresses]
        if len(families) != len(set(families)):
            raise ValueError(
                f'the issuer {self.url!r} is at two addresses of one family, and a pod dials one of them: '
                f'{[str(address) for address in self.addresses]}'
            )

    @property
    def host(self) -> str:
        """The name the issuer is at, which a client's pod resolves."""
        return urlsplit(self.url).hostname or ''


def _is_address(host: str) -> bool:
    try:
        _ = ip_address(host)
    except ValueError:
        return False
    return True


def portal_row(exposure: Exposure) -> Route:
    """The portal's own row of its exposure: the one route on `internet`, in the primary zone, ungated, with a home side on `lan`.

    The primary zone is where the portal sets its session cookie. A gate on
    the row would send a browser to the portal to reach the portal. The home
    side is what answers the name at home, for a browser through the `dns`
    stack's rewrite and for a client's pod through `issuer`, which a client
    of a portal on `internet` alone reaches only through the uplink.
    """
    rows = [held.row if isinstance(held, Held) else held for held in exposure.routes]
    served = [row for row in rows if isinstance(row, Route) and row.internet is not None]
    if len(served) != 1 or ZONE_PRIMARY not in served[0].zones:
        raise ValueError(
            'the portal is reached at one route on `internet`, in the primary zone, where its session cookie is set'
        )
    (row,) = served
    if isinstance(row.internet, Proxied) and row.internet.gate is not None:
        raise ValueError(f'the portal at {row.host!r} is gated by itself, so nobody could reach it to sign in')
    if row.lan is None:
        raise ValueError(
            f'the portal at {row.host!r} has no home side on `lan`, so neither a browser at home nor a client in the '
            'cluster reaches it while the uplink is down'
        )
    return row


def issuer(exposure: Exposure) -> Issuer:
    """The issuer of the portal at `exposure`: its row's name in the primary zone, over HTTPS, at the `lan` VIP.

    The addresses are the `lan` VIP in both families, the answer the `dns`
    stack's rewrite gives the name at home. While the legacy VPS holds the
    row (`Held`) there are none, as there is no rewrite: the name resolves
    where the legacy side publishes it, for a pod as for a browser, until
    the cut.
    """
    row = portal_row(exposure)
    held = any(isinstance(it, Held) and it.row == row for it in exposure.routes)
    vip = LAN_POOL.default_vip
    return Issuer(f'https://{row.host}.{ZONE_PRIMARY}', () if held else (vip.v4, vip.v6))


#: The two groups every client admits, each beside a group of its own.
_ADMINS = 'admins'
_USERS = 'users'

#: Every client the portal registers.
CLIENTS: tuple[Client, ...] = (
    Client(
        'immich',
        'photos',
        ('/auth/login', '/user-settings', '/api/oauth/mobile-redirect'),
        policy=(
            Grant(_ADMINS, Factor.TWO),
            Grant(_USERS, Factor.TWO),
            Grant('immich-users', Factor.TWO),
        ),
        token_endpoint_auth=TokenEndpointAuth.POST,
        scopes=(Scope.OPENID, Scope.EMAIL, Scope.PROFILE),
        native=('app.immich:///oauth-callback',),
    ),
    Client(
        'jellyfin',
        'tube',
        ('/sso/OID/redirect/Authelia',),
        policy=(
            Grant(_ADMINS, Factor.TWO),
            Grant(_USERS, Factor.ONE),
            Grant('jellyfin-users', Factor.ONE),
        ),
        token_endpoint_auth=TokenEndpointAuth.POST,
        scopes=(Scope.OPENID, Scope.PROFILE, Scope.GROUPS),
        pkce=True,
    ),
    Client(
        'grafana',
        'mon',
        ('/login/generic_oauth',),
        policy=(
            Grant(_ADMINS, Factor.TWO),
            Grant(_USERS, Factor.TWO),
            Grant('grafana-users', Factor.TWO),
        ),
        token_endpoint_auth=TokenEndpointAuth.BASIC,
        scopes=(Scope.OPENID, Scope.PROFILE, Scope.GROUPS, Scope.EMAIL, Scope.OFFLINE_ACCESS),
        pkce=True,
    ),
    # SplitPro's NextAuth provider sends no PKCE challenge and builds its
    # profile from the ID token alone; the email is the only key it matches
    # people by, and the name seeds a new user's display name. Expenses are
    # shared with people who have no business in `users`, so it admits a
    # group of its own, at one factor.
    Client(
        'splitpro',
        'split',
        # NextAuth derives the path from the provider's lowercased name.
        ('/api/auth/callback/authelia',),
        policy=(
            Grant(_ADMINS, Factor.ONE),
            Grant(_USERS, Factor.ONE),
            Grant('splitpro-users', Factor.ONE),
        ),
        token_endpoint_auth=TokenEndpointAuth.BASIC,
        scopes=(Scope.OPENID, Scope.PROFILE, Scope.EMAIL),
        id_token=(Claim.EMAIL, Claim.EMAIL_VERIFIED, Claim.NAME),
    ),
    # The homeserver provisions an account for whoever the portal lets
    # through, so this policy is the only gate on who gets one; one factor,
    # because a Matrix client takes this flow on every new device. It reads
    # its claims from the ID token alone: the localpart, the display name and
    # the email.
    Client(
        'matrix',
        'matrix',
        ('/_continuwuity/oidc/complete',),
        policy=(
            Grant(_ADMINS, Factor.ONE),
            Grant(_USERS, Factor.ONE),
            Grant('matrix-users', Factor.ONE),
        ),
        token_endpoint_auth=TokenEndpointAuth.BASIC,
        scopes=(Scope.OPENID, Scope.PROFILE, Scope.EMAIL),
        id_token=(Claim.PREFERRED_USERNAME, Claim.NAME, Claim.EMAIL),
    ),
)
