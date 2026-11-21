"""The portal: Authelia, its sessions' Redis, and the authentication canary with its target (rfc-008 §5.2).

Authelia is two things to the installation. It is the OpenID Connect provider
every client of `conventions.sso.CLIENTS` signs in through, and it is the
portal the gate asks (`kluster.lib.k8s.PORTAL`): the ExternalAuth filter on a
gated route asks one of its `ExtAuthz` endpoints before the request goes on.
This component serves what `PORTAL` names -- its namespace, the Service and
port, and each form's endpoint, whose name is the last element of the form's
path (`gate_endpoints`) -- and the `ReferenceGrant` that lets a gated route in
another namespace name that Service (declarative/cluster-infra.md §2).

**The namespace is the legacy server's**, `serving-system`, because the
ported ciphertexts open there alone (rfc-008 §4.6): `SECRETS`, the server's
own secrets, and `CONFIG_SECRETS`, the OpenID Connect provider's key and each
client's identifier and secret, both ported as they are; and the storage
encryption key, `conventions.sealed.AUTHELIA_STORAGE_KEY`, sealed strict from
the escrow and read from a Secret of its own. The server reads its own secrets
as `AUTHELIA_<key>_FILE` variables naming a mounted file each, and the
provider's through its configuration's `secret` template function, which
reads a mounted file the same way: the configuration, a ConfigMap, holds no
secret.

**The configuration is rendered from the component**
(`templates/configuration.yml.j2`): every client of the census with its
authorization policy, named after it, which denies whoever none of its grants
holds (`default_policy: deny`, since Authelia's own default is `two_factor`),
and its claims policy where it reads claims from the ID token; the
access-control rules, `ACCESS_RULES` and the canary's, keyed by route and
denying by default; and the notifier, plain SMTP to the relay
(`kluster.components.apps.exim`), whose admission of a sender is the label
the server's pods carry, `conventions.MAIL_SENDER_LABEL`.

**What the gate does not guard is held here** (`Portal`):

-   An access rule names a subject, a group or a user, and a factor, and
    nothing else (`AccessRule`): no rule admits a subject the portal has not
    signed in, so no answer of the portal's lets a client's own identity
    headers through to an application that trusts them, since Authelia sets
    them only for a signed-in subject.
-   No rule and no grant names a network, and regulation counts failed
    sign-ins by user alone: the first element of `X-Forwarded-For` the gate
    hands the portal is the client's own word.

**The users and the second factors live on one claim**, the users file and the
SQLite database, `PRECIOUS` and protected: the database's second factors and
OpenID Connect data are encrypted with the storage key, which the escrow
holds. One server owns the database at a time. **The sessions live in a Redis
with no claim**: a restart of it signs everyone in again, which is all it
costs (rfc-008 §4.2).

**The canary** is the standing check that the gate fails closed
(declarative/cluster-infra.md §2), in a namespace of its own,
`CANARY_NAMESPACE`, which the grant names as it names every gated
namespace: its route reaches the portal across namespaces, as a gated route
does. Its target is a static responder behind that route on the portal's
name, whose path `CANARY_PATH` the session form of the gate guards
(`kluster.lib.k8s.gate_filter`), and an access rule admits the
administrators alone to it. The CronJob `auth-canary` requests that path with
no credential at the balancer's IPv4, which Cilium answers on the caller's
own node, under the portal's name, so it answers the same before the portal's
cut as after; a run passes on a refusal alone, a 401 or a redirect to the
portal (`templates/auth-canary.sh`). The scheduled-job family's rules over
`AUTH_CANARY` are its alerts, declared beside it: a run that was not refused
fails, and a canary that stops running goes stale.

**The portal keeps working while the uplink is down**, which it declares
(`kluster.components.apps.UPLINK_DOWN`): a client at home signs in through an
outage of the home uplink, so every pod here runs at the home site, the
canary's included, at the held priority and through its node's
unreachability. The canary reads the gate on whatever node it runs on, since
every node's Envoy carries the same `internet-gw` configuration. Its route
has a home side on `lan`, which the component refuses an exposure without
(`conventions.sso.portal_row`): the `dns` stack rewrites the portal's name to
the `lan` VIP for a browser at home, and an OpenID Connect client's pod dials
the name at the same address (`conventions.sso.issuer`). Each pod
carries CPU and memory limits. The server's and the receiver's state the
claim's constraints, which compose its hold in, and run in its blockio
class; the claim's movers bind it at home, where the pod runs.
The server's, Redis's and the curl image's users are stated as numbers
(rfc-008 §10.2). Nothing here calls the Kubernetes API.

**Two migration layers** are the component's own, each described where it is
declared below: the receiver the land streams the legacy claim into
(`receiving`), and the endpoints the legacy cluster's forward-auth checks ask
until the last gated legacy front is cut.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import pulumi
import pulumi_crds as crds
import pulumi_kubernetes as k8s

from kluster import conventions
from kluster.components.apps import Backend, Exposed, Fronts, declared_constraints, expose
from kluster.components.apps.exim import HOST as SMTP_HOST
from kluster.components.apps.exim import PORT as SMTP_PORT
from kluster.components.apps.storage import Claim, claim
from kluster.components.monitoring.operator_objects import operator_object
from kluster.components.monitoring.rules import RuleGroup, rule_group_spec, scheduled_job_rules
from kluster.conventions.alert import Playbook
from kluster.conventions.backup import PRECIOUS
from kluster.conventions.routes import Exposure, Gate
from kluster.conventions.schedule import ScheduledJob
from kluster.conventions.sso import Factor
from kluster.lib import templates
from kluster.lib.k8s import (
    PORTAL,
    ApplicationNamespace,
    Constraints,
    Portal,
    PortedSecret,
    application_namespace,
    gate_filter,
    sealed_value,
)
from kluster.lib.versions import ImagePin
from putils import Component

__all__ = (
    'ACCESS_RULES',
    'AUTHELIA_USER',
    'AUTH_CANARY',
    'CANARY_NAMESPACE',
    'CANARY_PATH',
    'CANARY_USER',
    'CONFIGURATION',
    'CONFIG_SECRETS',
    'DATA',
    'LABELS',
    'NAME',
    'NAMESPACE',
    'RECEIVE',
    'REDIS_USER',
    'SECRETS',
    'AccessRule',
    'Authelia',
    'Group',
    'User',
    'gate_endpoints',
    'portal_configuration',
    'portal_url',
)

#: The application's name: its Deployment and its configuration.
NAME = 'authelia'

#: The namespace the portal is served in, which the ported ciphertexts are
#: sealed to and `conventions.backup.BACKED_NAMESPACES` names.
NAMESPACE = PORTAL.namespace

#: The labels the server's pods carry and its Service selects.
LABELS: Mapping[str, str] = {'app.kubernetes.io/name': NAME}

#: The user and group the server runs as, and so the owner of everything on
#: its claim, which the claim's movers read as. The image starts the server
#: as whoever runs it.
AUTHELIA_USER = 1000

#: The legacy Secret of the server's own secrets, ported as it is: each data
#: key reaches the server as `AUTHELIA_<key>_FILE`. The storage key is not
#: among them (`conventions.sealed.AUTHELIA_STORAGE_KEY`).
SECRETS = PortedSecret(
    name='authelia',
    namespace=NAMESPACE,
    keys=(
        'IDENTITY_PROVIDERS_OIDC_HMAC_SECRET',
        'IDENTITY_VALIDATION_RESET_PASSWORD_JWT_SECRET',
        'SESSION_REDIS_PASSWORD',
        'SESSION_SECRET',
    ),
)
_REDIS_PASSWORD = 'SESSION_REDIS_PASSWORD'

#: The OpenID Connect provider's signing key.
_JWKS_KEY = 'OIDC_JWKS_ISSUER_PRIVATE_KEY'


def _client_key(client: conventions.sso.Client, part: str) -> str:
    """The data key of `CONFIG_SECRETS` holding a client's identifier (`ID`) or secret (`SECRET`)."""
    return f'OIDC_CLIENTS_{client.name.upper()}_{part}'


#: The legacy Secret of the provider's key and each client's identifier and
#: secret, ported as it is, under the keys the legacy configuration read.
CONFIG_SECRETS = PortedSecret(
    name='authelia-config',
    namespace=NAMESPACE,
    keys=(
        _JWKS_KEY,
        *(_client_key(client, part) for client in conventions.sso.CLIENTS for part in ('ID', 'SECRET')),
    ),
)

#: Where each Secret is mounted, a directory of one file per data key.
_SECRETS_DIRECTORY = '/secrets/authelia'
_CONFIG_SECRETS_DIRECTORY = '/secrets/authelia-config'
_STORAGE_KEY_DIRECTORY = '/secrets/storage-key'

#: Where the configuration is mounted, and the file it lands under in its
#: ConfigMap.
CONFIGURATION = '/config/configuration.yml'
_CONFIG_FILE = 'configuration.yml'

#: Where the claim is mounted: the users file and the database.
DATA = '/storage'
_USERS = f'{DATA}/users.yaml'
_DATABASE = f'{DATA}/db.sqlite3'

#: The claim's base name, and its size in GiB: the legacy claim holds about a
#: megabyte.
_CLAIM = 'storage'
_CLAIM_GIB = 1

#: The Redis that holds the sessions: its name, labels, port, and user and
#: group, the official image's `redis`, by number.
_REDIS = f'{NAME}-redis'
_REDIS_LABELS: Mapping[str, str] = {'app.kubernetes.io/name': _REDIS}
_REDIS_PORT = 6379
REDIS_USER = 999


@dataclass(frozen=True)
class Group:
    """A group of the portal's users, as a subject of an access rule."""

    name: str

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError('a group names no group, so a rule on it would name no subject')


@dataclass(frozen=True)
class User:
    """One of the portal's users, as a subject of an access rule."""

    name: str

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError('a user names no user, so a rule on it would name no subject')


def _subject(subject: Group | User) -> str:
    """A subject as Authelia's configuration spells it."""
    match subject:
        case Group(name):
            return f'group:{name}'
        case User(name):
            return f'user:{name}'


@dataclass(frozen=True)
class AccessRule:
    """One rule of the portal's access control: who may pass a gate on some names, with how many factors.

    It names a subject and never a network, and its policy is a factor: no
    rule lets a subject through that the portal has not signed in, which is
    what keeps a client's own identity headers away from an application that
    trusts them (`kluster.lib.k8s.Portal`).
    """

    hosts: tuple[str, ...]
    """The labels the rule matches, each under the primary zone."""
    subject: Group | User
    factor: Factor
    resources: tuple[str, ...] = ()
    """The paths the rule is narrowed to, as Authelia's regular expressions;
    every path where empty."""

    def __post_init__(self) -> None:
        if not self.hosts:
            raise ValueError('an access rule names no host')


#: The access-control rules of the names behind the gate, ported from the
#: legacy portal. A name a legacy forward-auth check guards is here too: it
#: reaches this portal from the cut on.
ACCESS_RULES: tuple[AccessRule, ...] = (
    AccessRule(('k8s', 'mon'), Group('admins'), Factor.TWO),
    AccessRule(('bt',), Group('users'), Factor.TWO),
    AccessRule(('sync', 'sync-nas'), Group('users'), Factor.TWO),
    AccessRule(('dav',), User('webdav'), Factor.ONE),
    AccessRule(('spool',), Group('users'), Factor.ONE),
)

#: The canary's namespace, which holds its target, its route, its CronJob and
#: its rules, and which the grant names beside every gated namespace.
CANARY_NAMESPACE = 'auth-canary'

#: Where the canary's target is served on the portal's name, behind the gate.
#: No path of Authelia's own starts with it.
CANARY_PATH = '/canary'

#: Who passes the gate in front of the canary's target: nobody the canary
#: is, since it presents no credential.
_CANARY_SUBJECT = Group('admins')

#: The canary's run. A run the controller cannot start by its starting
#: deadline is skipped rather than run late, so the next run is its retry.
AUTH_CANARY = ScheduledJob(
    alert='AuthCanary',
    namespace=CANARY_NAMESPACE,
    cronjob='auth-canary',
    period=dt.timedelta(minutes=5),
    deadline=dt.timedelta(minutes=1),
    starting_deadline=dt.timedelta(seconds=30),
    playbook=Playbook('docs/operations.md', '10.2'),
)
_CANARY_SCHEDULE = '*/5 * * * *'

#: The canary's labels, its run's file in its ConfigMap, and the user and
#: group the curl image's `curl_user` is, by number.
_CANARY_LABELS: Mapping[str, str] = {'app.kubernetes.io/name': AUTH_CANARY.cronjob}
_CANARY_SCRIPT = 'auth-canary.sh'
_CANARY_DIRECTORY = '/canary'
CANARY_USER = 100
_CANARY_GROUP = 101

#: The canary's target: a stock static server, which answers whatever passes
#: the gate, on the unprivileged image's port.
_TARGET = f'{AUTH_CANARY.cronjob}-target'
_TARGET_LABELS: Mapping[str, str] = {'app.kubernetes.io/name': _TARGET}
_TARGET_PORT = 8080

#: What every container here is held to.
_CONTAINER_SECURITY = k8s.core.v1.SecurityContextArgs(
    allow_privilege_escalation=False,
    read_only_root_filesystem=True,
    capabilities=k8s.core.v1.CapabilitiesArgs(drop=['ALL']),
)


def _pod_security(user: int, group: int | None = None) -> k8s.core.v1.PodSecurityContextArgs:
    """Who a pod runs as: `user`, and `group`, its own number unless the image gave it another."""
    return k8s.core.v1.PodSecurityContextArgs(
        run_as_non_root=True,
        run_as_user=user,
        run_as_group=user if group is None else group,
        fs_group=user if group is None else group,
        seccomp_profile=k8s.core.v1.SeccompProfileArgs(type='RuntimeDefault'),
    )


# -- the configuration -----------------------------------------------------------


@dataclass(frozen=True)
class _Strategy:
    """How an authorization endpoint reads the subject: Authelia's strategy name, and the schemes it is held to."""

    name: str
    schemes: tuple[str, ...] = ()


@dataclass(frozen=True)
class _Endpoint:
    """An authorization endpoint, served at `/api/authz/<name>`, and the strategies it reads the subject by."""

    name: str
    implementation: str
    strategies: tuple[_Strategy, ...]


#: The strategy each form of the gate asks the portal by: the session cookie,
#: or the `Authorization` header with the Basic scheme alone.
_GATE_STRATEGIES: Mapping[Gate, _Strategy] = {
    Gate.SESSION: _Strategy('CookieSession'),
    Gate.BASIC: _Strategy('HeaderAuthorization', ('Basic',)),
}

#: The prefix Authelia serves its authorization endpoints under, each at its
#: name.
_AUTHZ_PREFIX = '/api/authz/'


def gate_endpoints(portal: Portal) -> tuple[_Endpoint, ...]:
    """The `ExtAuthz` endpoint of each form of the gate, at the path `portal` names for it.

    Envoy asks the endpoint at the form's path with the request's own path
    after it, so the path ends in `/`: Authelia reads what follows the
    endpoint's name as the path of the request it authorizes, and with no
    `/` between them it reads no path at all. A path outside the prefix, or
    one without the trailing `/`, is refused.
    """
    found: list[_Endpoint] = []
    for gate, path in portal.paths.items():
        if not (path.startswith(_AUTHZ_PREFIX) and path.endswith('/')):
            raise ValueError(f'the portal serves no authorization endpoint at {path}')
        name = path.removeprefix(_AUTHZ_PREFIX).rstrip('/')
        found.append(_Endpoint(name, 'ExtAuthz', (_GATE_STRATEGIES[gate],)))
    return tuple(found)


# -- the forward-auth layer ------------------------------------------------------
#
# The legacy cluster's forward-auth checks, Traefik `forwardAuth` middlewares
# in front of the gated legacy fronts, ask this portal from Wave A1's cut on,
# at its public name (rfc-008 §3.5): the session check at
# `/api/authz/forward-auth`, and the Basic check at
# `/api/authz/forward-auth-basic`, each naming the request in
# `X-Forwarded-Host` and `X-Forwarded-Uri` beside the portal's own `Host`.
# Declaring any endpoint replaces Authelia's default ones, so the two are
# declared here beside the gate's, each `ForwardAuth` on the strategy of the
# gate's form it stands for. `ForwardAuth` reads the request from the
# forwarded headers alone, which Traefik's entry point takes off a client's
# request and writes itself, so a client names no request of its own.
#
# The layer is `_FORWARD_AUTH_ENDPOINTS` here, with its use in
# `portal_configuration`; and in `tests/test_apps_authelia.py`,
# `TRAEFIK_HEADERS`, `FORWARD_AUTH`, `FORWARD_AUTH_BASIC` and `_traefik`, the
# `forward-auth-*` entries of `_asks`, `Ask.basic`, which only they set, the
# `webdav` user of `USERS`, which only they sign in as, the module
# docstring's words on Traefik and kluster-code, and the cases under "the
# forward-auth layer". It goes when
# nothing asks it: kluster-code drops the checks' transitional target with the
# last gated legacy front, `spool`, at Wave B's cut (rfc-008 §10.3, slice 38).

_FORWARD_AUTH_ENDPOINTS: tuple[_Endpoint, ...] = (
    _Endpoint('forward-auth', 'ForwardAuth', (_GATE_STRATEGIES[Gate.SESSION],)),
    _Endpoint('forward-auth-basic', 'ForwardAuth', (_GATE_STRATEGIES[Gate.BASIC],)),
)


@dataclass(frozen=True)
class _Rule:
    domains: tuple[str, ...]
    resources: tuple[str, ...]
    subject: str
    policy: str


@dataclass(frozen=True)
class _Grant:
    policy: str
    subject: str


@dataclass(frozen=True)
class _Client:
    name: str
    id: str
    secret: str
    grants: tuple[_Grant, ...]
    id_token: tuple[str, ...]
    pkce: bool
    token_endpoint_auth: str
    scopes: tuple[str, ...]
    grant_types: tuple[str, ...]
    redirect_uris: tuple[str, ...]


@dataclass(frozen=True)
class _Configuration:
    """What `templates/configuration.yml.j2` reads."""

    address: str
    endpoints: tuple[_Endpoint, ...]
    zone: str
    users: str
    redis_host: str
    redis_port: int
    portal: str
    landing: str
    database: str
    smtp: str
    identifier: str
    sender: str
    rules: tuple[_Rule, ...]
    jwks_key: str
    clients: tuple[_Client, ...]
    claims: tuple[_Client, ...]


#: Where the template puts a value under a key of an item of `clients` or of
#: `jwks`: the indentation a value of several lines is given as a block.
_ITEM_VALUE_INDENT = 10


def _secret(key: str) -> str:
    """The template expression Authelia replaces with the file of `CONFIG_SECRETS`' `key`, as a value of an item.

    `mindent` makes a value of several lines a block scalar at
    `_ITEM_VALUE_INDENT`, and `msquote` quotes a value of one line.
    """
    return f'{{{{ secret "{_CONFIG_SECRETS_DIRECTORY}/{key}" | mindent {_ITEM_VALUE_INDENT} "|" | msquote }}}}'


def _client(client: conventions.sso.Client) -> _Client:
    refresh = conventions.sso.Scope.OFFLINE_ACCESS in client.scopes
    return _Client(
        name=client.name,
        id=_secret(_client_key(client, 'ID')),
        secret=_secret(_client_key(client, 'SECRET')),
        grants=tuple(_Grant(grant.factor.value, _subject(Group(grant.group))) for grant in client.policy),
        id_token=tuple(claim.value for claim in client.id_token),
        pkce=client.pkce,
        token_endpoint_auth=client.token_endpoint_auth.value,
        scopes=tuple(scope.value for scope in client.scopes),
        # The scope that asks for a refresh token is granted only to a client
        # that may use the refresh grant.
        grant_types=('refresh_token', 'authorization_code') if refresh else ('authorization_code',),
        redirect_uris=conventions.sso.redirect_uris(client),
    )


def _rule(rule: AccessRule) -> _Rule:
    return _Rule(
        domains=tuple(f'{host}.{conventions.ZONE_PRIMARY}' for host in rule.hosts),
        resources=rule.resources,
        subject=_subject(rule.subject),
        policy=rule.factor.value,
    )


def _portal_host(exposure: Exposure) -> str:
    return conventions.sso.issuer(exposure).host


def portal_url(exposure: Exposure) -> str:
    """Where the portal is, as a browser is sent to it: the issuer its clients validate, what its configuration names it and the canary's redirect is held to."""
    return conventions.sso.issuer(exposure).url


def _canary_rule(host: str) -> AccessRule:
    return AccessRule((host,), _CANARY_SUBJECT, Factor.ONE, resources=(f'^{CANARY_PATH}([/?].*)?$',))


def portal_configuration(exposure: Exposure) -> str:
    """Authelia's configuration, rendered for the portal at `exposure`'s row, ahead of Authelia's own `template` filter."""
    portal = portal_url(exposure)
    canary = _canary_rule(conventions.sso.portal_row(exposure).host)
    clients = tuple(_client(client) for client in conventions.sso.CLIENTS)
    return templates.render(
        'kluster.components.apps.authelia',
        'templates/configuration.yml.j2',
        _Configuration(
            address=f'tcp://:{PORTAL.port}/',
            endpoints=(*gate_endpoints(PORTAL), *_FORWARD_AUTH_ENDPOINTS),
            zone=conventions.ZONE_PRIMARY,
            users=_USERS,
            redis_host=f'{_REDIS}.{NAMESPACE}.svc',
            redis_port=_REDIS_PORT,
            portal=portal,
            # Where a browser that signed in with nowhere to go lands.
            landing=f'{portal}/settings',
            database=_DATABASE,
            smtp=f'smtp://{SMTP_HOST}:{SMTP_PORT}',
            identifier=_portal_host(exposure),
            sender=f'auth@{conventions.ZONE_PRIMARY}',
            rules=tuple(_rule(rule) for rule in (*ACCESS_RULES, canary)),
            jwks_key=_secret(_JWKS_KEY),
            clients=clients,
            claims=tuple(client for client in clients if client.id_token),
        ),
    )


# -- the server ------------------------------------------------------------------


def _secret_volume(name: str, secret: pulumi.Input[str]) -> k8s.core.v1.VolumeArgs:
    # Each file is the pod's group's to read alone.
    return k8s.core.v1.VolumeArgs(
        name=name, secret=k8s.core.v1.SecretVolumeSourceArgs(secret_name=secret, default_mode=0o440)
    )


def _data_volume(storage: Claim) -> k8s.core.v1.VolumeArgs:
    return k8s.core.v1.VolumeArgs(
        name='storage',
        persistent_volume_claim=k8s.core.v1.PersistentVolumeClaimVolumeSourceArgs(
            claim_name=storage.claim.metadata.name
        ),
    )


def _server(
    image: ImagePin, secrets: pulumi.Input[str], config_secrets: pulumi.Input[str], storage: Claim
) -> k8s.core.v1.PodSpecArgs:
    """The server's pod: Authelia, reading its configuration, its Secrets and its claim, placed where the claim is."""
    files = {key: f'{_SECRETS_DIRECTORY}/{key}' for key in SECRETS.keys}
    (storage_key,) = conventions.sealed.AUTHELIA_STORAGE_KEY.keys
    files[storage_key] = f'{_STORAGE_KEY_DIRECTORY}/{storage_key}'
    probe = k8s.core.v1.HTTPGetActionArgs(path='/api/health', port='http')
    container = k8s.core.v1.ContainerArgs(
        name=NAME,
        image=str(image),
        command=['authelia'],
        args=['--config', CONFIGURATION],
        ports=[k8s.core.v1.ContainerPortArgs(name='http', container_port=PORTAL.port, protocol='TCP')],
        env=[
            # The configuration is a template Authelia renders as it reads it.
            k8s.core.v1.EnvVarArgs(name='X_AUTHELIA_CONFIG_FILTERS', value='template'),
            *(k8s.core.v1.EnvVarArgs(name=f'AUTHELIA_{key}_FILE', value=path) for key, path in sorted(files.items())),
        ],
        startup_probe=k8s.core.v1.ProbeArgs(http_get=probe, period_seconds=5, failure_threshold=12),
        readiness_probe=k8s.core.v1.ProbeArgs(http_get=probe, period_seconds=5),
        liveness_probe=k8s.core.v1.ProbeArgs(http_get=probe, period_seconds=30, failure_threshold=5),
        resources=k8s.core.v1.ResourceRequirementsArgs(
            # Its working set with its Redis runs at 0.07 GiB and peaks at 0.40
            # over 30 days (nodes.md §1.1): a gated application's every request
            # is a round trip here, and a burst of them is what the peak is.
            requests={'cpu': '50m', 'memory': '96Mi'},
            limits={'cpu': '200m', 'memory': '512Mi'},
        ),
        security_context=_CONTAINER_SECURITY,
        volume_mounts=[
            k8s.core.v1.VolumeMountArgs(name='config', mount_path=CONFIGURATION, sub_path=_CONFIG_FILE, read_only=True),
            k8s.core.v1.VolumeMountArgs(name='secrets', mount_path=_SECRETS_DIRECTORY, read_only=True),
            k8s.core.v1.VolumeMountArgs(name='config-secrets', mount_path=_CONFIG_SECRETS_DIRECTORY, read_only=True),
            k8s.core.v1.VolumeMountArgs(name='storage-key', mount_path=_STORAGE_KEY_DIRECTORY, read_only=True),
            k8s.core.v1.VolumeMountArgs(name='storage', mount_path=DATA),
        ],
    )
    return k8s.core.v1.PodSpecArgs(
        automount_service_account_token=False,
        # A Service's link variables would land beside the server's own, and
        # Authelia reads every `AUTHELIA_` variable as configuration.
        enable_service_links=False,
        **storage.constraints.pod_fields(),
        security_context=_pod_security(AUTHELIA_USER),
        containers=[container],
        volumes=[
            k8s.core.v1.VolumeArgs(name='config', config_map=k8s.core.v1.ConfigMapVolumeSourceArgs(name=NAME)),
            _secret_volume('secrets', secrets),
            _secret_volume('config-secrets', config_secrets),
            _secret_volume('storage-key', conventions.sealed.AUTHELIA_STORAGE_KEY.name),
            _data_volume(storage),
        ],
    )


# -- the receiving layer ---------------------------------------------------------
#
# The land streams the legacy server's claim into this one, after the legacy
# server has stopped (rfc-008 §5.2), through a pod that mounts the claim. The
# stream's two ends, from the legacy claim's directory to this claim's mount:
#
#     tar -c -C <legacy claim> . |
#         kubectl exec -i -n serving-system deploy/authelia -- tar -x --strip-components=1 -C /storage
#
# The receiving end is `RECEIVE`. `--strip-components=1` drops the stream's
# `./`, which is root's, as the claim's own directory is: the receiver, which
# is not root, can set neither its time nor its mode. The server started on an
# empty claim would create a database of its own there, so under `receiving`
# the Deployment runs a receiver in the server's place: an idle container of
# the server's own image, which has `tar`, mounting the claim where the server
# does. A second merge, without `receiving`, starts the server on what was
# streamed. The receiver is a running pod with the server's constraints, never
# zero replicas, because the claim binds to the node its first consumer is
# scheduled on, and its backup's mover, the first consumer otherwise, selects
# no node. It runs as the server's user, so what it extracts is the server's
# to read and write.
#
# While it runs, the portal's Service has no endpoint, so the gate fails
# closed for every request and the canary fails every run by design:
# `AuthCanaryFailed` fires through the window, and the canary green that
# Wave A1 verifies is read after the second merge (operations.md §10.2).
#
# The layer is everything in this module and in `tests/test_apps_authelia.py`
# that names the receiver or `receiving`. Today that is the `receiving`
# parameter of `Authelia`, `_receiver`, `_RECEIVER_COMMAND` and `RECEIVE`
# here, with its entry in `__all__`; and there the `receiving` parameter of
# `declare`, the `receiving` fixture, the `RECEIVE` import and the cases under
# "the receiving layer". It goes with Wave A1's cut, once the server has
# started on the streamed claim, since nothing streams into it after that
# (rfc-008 §10.3, slice 29).

#: What the receiver runs: nothing, until the land execs `tar` beside it.
_RECEIVER_COMMAND = ('sleep', 'infinity')

#: What the land execs in the receiver, reading the stream on its standard
#: input: `tar -c -C <legacy claim> .` at the other end.
RECEIVE = ('tar', '-x', '--strip-components=1', '-C', DATA)


def _receiver(image: ImagePin, storage: Claim) -> k8s.core.v1.PodSpecArgs:
    """The receiver's pod: an idle container of the server's image, mounting the claim at `DATA`, placed and run as the server is."""
    container = k8s.core.v1.ContainerArgs(
        name='receiver',
        image=str(image),
        command=list(_RECEIVER_COMMAND),
        resources=k8s.core.v1.ResourceRequirementsArgs(
            requests={'cpu': '10m', 'memory': '16Mi'},
            limits={'cpu': '100m', 'memory': '64Mi'},
        ),
        security_context=_CONTAINER_SECURITY,
        volume_mounts=[k8s.core.v1.VolumeMountArgs(name='storage', mount_path=DATA)],
    )
    return k8s.core.v1.PodSpecArgs(
        automount_service_account_token=False,
        enable_service_links=False,
        **storage.constraints.pod_fields(),
        security_context=_pod_security(AUTHELIA_USER),
        containers=[container],
        volumes=[_data_volume(storage)],
    )


# -- the sessions ----------------------------------------------------------------


def _redis(image: ImagePin, secrets: pulumi.Input[str], constraints: Constraints) -> k8s.core.v1.PodSpecArgs:
    """Redis's pod: the sessions in memory alone, behind the password the server's Secret carries."""
    container = k8s.core.v1.ContainerArgs(
        name='redis',
        image=str(image),
        command=['redis-server'],
        # The kubelet expands a variable defined in the container's `env`. No
        # snapshot and no append-only file: the sessions are not kept across a
        # restart.
        args=['--requirepass', '$(REDIS_PASSWORD)', '--save', '', '--appendonly', 'no'],
        ports=[k8s.core.v1.ContainerPortArgs(name='redis', container_port=_REDIS_PORT, protocol='TCP')],
        env=[
            k8s.core.v1.EnvVarArgs(
                name='REDIS_PASSWORD',
                value_from=k8s.core.v1.EnvVarSourceArgs(
                    secret_key_ref=k8s.core.v1.SecretKeySelectorArgs(name=secrets, key=_REDIS_PASSWORD)
                ),
            )
        ],
        readiness_probe=k8s.core.v1.ProbeArgs(
            tcp_socket=k8s.core.v1.TCPSocketActionArgs(port='redis'), period_seconds=10
        ),
        resources=k8s.core.v1.ResourceRequirementsArgs(
            requests={'cpu': '10m', 'memory': '16Mi'},
            limits={'cpu': '100m', 'memory': '64Mi'},
        ),
        security_context=_CONTAINER_SECURITY,
        volume_mounts=[k8s.core.v1.VolumeMountArgs(name='data', mount_path='/data')],
    )
    return k8s.core.v1.PodSpecArgs(
        automount_service_account_token=False,
        enable_service_links=False,
        **constraints.pod_fields(),
        security_context=_pod_security(REDIS_USER),
        containers=[container],
        # Its working directory, which it writes nothing to it keeps.
        volumes=[k8s.core.v1.VolumeArgs(name='data', empty_dir=k8s.core.v1.EmptyDirVolumeSourceArgs())],
    )


# -- the canary ------------------------------------------------------------------


def _target(image: ImagePin) -> k8s.core.v1.ContainerArgs:
    return k8s.core.v1.ContainerArgs(
        name='target',
        image=str(image),
        ports=[k8s.core.v1.ContainerPortArgs(name='http', container_port=_TARGET_PORT, protocol='TCP')],
        readiness_probe=k8s.core.v1.ProbeArgs(
            tcp_socket=k8s.core.v1.TCPSocketActionArgs(port='http'), period_seconds=30
        ),
        resources=k8s.core.v1.ResourceRequirementsArgs(
            requests={'cpu': '5m', 'memory': '8Mi'},
            limits={'cpu': '50m', 'memory': '32Mi'},
        ),
        security_context=_CONTAINER_SECURITY,
        # The server writes its pid file and its caches under `/tmp`.
        volume_mounts=[k8s.core.v1.VolumeMountArgs(name='tmp', mount_path='/tmp')],
    )


def _canary_job(
    image: ImagePin, *, portal: str, address: pulumi.Input[str], constraints: Constraints
) -> k8s.batch.v1.JobTemplateSpecArgs:
    container = k8s.core.v1.ContainerArgs(
        name='canary',
        image=str(image),
        command=['sh', f'{_CANARY_DIRECTORY}/{_CANARY_SCRIPT}'],
        env=[
            k8s.core.v1.EnvVarArgs(name='CANARY_HOST', value=portal.removeprefix('https://')),
            k8s.core.v1.EnvVarArgs(name='CANARY_ADDRESS', value=address),
            k8s.core.v1.EnvVarArgs(name='CANARY_PATH', value=CANARY_PATH),
            k8s.core.v1.EnvVarArgs(name='CANARY_PORTAL', value=portal),
        ],
        resources=k8s.core.v1.ResourceRequirementsArgs(
            requests={'cpu': '5m', 'memory': '8Mi'},
            limits={'cpu': '100m', 'memory': '32Mi'},
        ),
        security_context=_CONTAINER_SECURITY,
        volume_mounts=[k8s.core.v1.VolumeMountArgs(name='run', mount_path=_CANARY_DIRECTORY, read_only=True)],
    )
    return k8s.batch.v1.JobTemplateSpecArgs(
        spec=k8s.batch.v1.JobSpecArgs(
            active_deadline_seconds=int(AUTH_CANARY.deadline.total_seconds()),
            # A run is one request: a retry is the next run.
            backoff_limit=0,
            template=k8s.core.v1.PodTemplateSpecArgs(
                metadata=k8s.meta.v1.ObjectMetaArgs(labels=dict(_CANARY_LABELS)),
                spec=k8s.core.v1.PodSpecArgs(
                    restart_policy='Never',
                    automount_service_account_token=False,
                    enable_service_links=False,
                    **constraints.pod_fields(),
                    security_context=_pod_security(CANARY_USER, _CANARY_GROUP),
                    containers=[container],
                    volumes=[
                        k8s.core.v1.VolumeArgs(
                            name='run',
                            config_map=k8s.core.v1.ConfigMapVolumeSourceArgs(name=AUTH_CANARY.cronjob),
                        )
                    ],
                ),
            ),
        )
    )


# -- the component ---------------------------------------------------------------


class Authelia(Component, pulumi_type='kluster:apps:Authelia'):
    """The portal's namespace, its Secrets, configuration and claim, its server, its sessions, what the gate names, the canary, and what its exposure derives.

    `exposure` is its row of the route census, which holds the portal's own
    route, on `internet` and on `lan` (`conventions.sso.portal_row`).
    `image`, `redis_image`, `canary_image` and `target_image` are the pins of
    the server, its Redis, the canary's curl and its target. `secrets`
    and `config_secrets` are the ciphertexts `kluster.lib.k8s.sealed_ciphertext`
    reads for the ported `SECRETS` and `CONFIG_SECRETS`, `storage_key` the one
    it reads for `conventions.sealed.AUTHELIA_STORAGE_KEY`, and `writer_key` the
    ciphertext of the namespace's sealed writer key, which the claim's backups
    carry. `gated` is each namespace whose routes the gate guards, which the
    grant names beside the canary's. `fronts` and `zone_ids` are what the
    program read for `expose`; the canary dials the balancer's IPv4 among the
    fronts. `receiving`, while the land streams the claim in, runs the
    receiver in the server's place (the receiving layer, above). The providers
    arrive in `opts`: the Kubernetes provider for the cluster's objects and the
    Cloudflare one for its record.
    """

    namespace: ApplicationNamespace
    canary_namespace: ApplicationNamespace
    secrets: crds.bitnami.v1alpha1.SealedSecret
    config_secrets: crds.bitnami.v1alpha1.SealedSecret
    storage_key: crds.bitnami.v1alpha1.SealedSecret
    config: k8s.core.v1.ConfigMap
    data: Claim
    deployment: k8s.apps.v1.Deployment
    service: k8s.core.v1.Service
    redis: k8s.apps.v1.Deployment
    redis_service: k8s.core.v1.Service
    grant: crds.gateway.v1.ReferenceGrant
    target: k8s.apps.v1.Deployment
    target_service: k8s.core.v1.Service
    canary_route: crds.gateway.v1.HTTPRoute
    canary_run: k8s.core.v1.ConfigMap
    canary: k8s.batch.v1.CronJob
    canary_rules: k8s.apiextensions.CustomResource
    exposed: Exposed

    def __init__(
        self,
        name: str,
        *,
        exposure: Exposure,
        image: ImagePin,
        redis_image: ImagePin,
        canary_image: ImagePin,
        target_image: ImagePin,
        secrets: Mapping[str, pulumi.Input[str]],
        config_secrets: Mapping[str, pulumi.Input[str]],
        storage_key: Mapping[str, pulumi.Input[str]],
        writer_key: Mapping[str, pulumi.Input[str]],
        gated: Sequence[str],
        fronts: Fronts,
        zone_ids: pulumi.Input[Mapping[str, str]],
        receiving: bool = False,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        configuration = portal_configuration(exposure)
        portal = portal_url(exposure)
        super().__init__(name, opts=opts)

        self.namespace = application_namespace(f'{name}-namespace', name=NAMESPACE, opts=self.child_opts())
        self.constraints = declared_constraints(exposure, self.namespace, uplink_down=True)
        self.canary_namespace = application_namespace(
            f'{name}-canary-namespace', name=CANARY_NAMESPACE, opts=self.child_opts()
        )
        # Read off each namespace, so each object in it waits for it.
        namespace = self.namespace.namespace.metadata.name
        canary_namespace = self.canary_namespace.namespace.metadata.name
        # A value or a claim names its namespace as a string, so its object
        # waits for it here.
        behind = self.child_opts(depends_on=[self.namespace.namespace])

        self.secrets = sealed_value(f'{name}-secrets', SECRETS, ciphertext=secrets, opts=behind)
        self.config_secrets = sealed_value(
            f'{name}-config-secrets', CONFIG_SECRETS, ciphertext=config_secrets, opts=behind
        )
        self.storage_key = sealed_value(
            f'{name}-storage-key', conventions.sealed.AUTHELIA_STORAGE_KEY, ciphertext=storage_key, opts=behind
        )
        self.config = k8s.core.v1.ConfigMap(
            name,
            metadata=k8s.meta.v1.ObjectMetaArgs(name=NAME, namespace=namespace),
            data={_CONFIG_FILE: configuration},
            opts=self.child_opts(),
        )
        self.data = claim(
            f'{name}-storage',
            _CLAIM,
            _CLAIM_GIB,
            PRECIOUS,
            namespace=NAMESPACE,
            uid=AUTHELIA_USER,
            writer_key=writer_key,
            constraints=self.constraints,
            protect=True,
            opts=behind,
        )
        self.deployment = k8s.apps.v1.Deployment(
            name,
            metadata=k8s.meta.v1.ObjectMetaArgs(
                name=NAME,
                namespace=namespace,
                # Reloader rolls the pod when its configuration or a Secret it
                # reads changes; the server reads them only at its start.
                annotations={'reloader.stakater.com/auto': 'true'},
            ),
            spec=k8s.apps.v1.DeploymentSpecArgs(
                replicas=1,
                # One server owns the database: a second pod beside the old
                # one would open it too.
                strategy=k8s.apps.v1.DeploymentStrategyArgs(type='Recreate'),
                selector=k8s.meta.v1.LabelSelectorArgs(match_labels=dict(LABELS)),
                template=k8s.core.v1.PodTemplateSpecArgs(
                    # The relay admits a sender by this label.
                    metadata=k8s.meta.v1.ObjectMetaArgs(
                        labels={**LABELS, **conventions.MAIL_SENDER_LABEL}, annotations=dict(self.data.pod_annotations)
                    ),
                    spec=_receiver(image, self.data)
                    if receiving
                    else _server(image, self.secrets.metadata.name, self.config_secrets.metadata.name, self.data),
                ),
            ),
            opts=self.child_opts(depends_on=[self.storage_key]),
        )
        self.service = k8s.core.v1.Service(
            name,
            metadata=k8s.meta.v1.ObjectMetaArgs(name=PORTAL.service, namespace=namespace),
            spec=k8s.core.v1.ServiceSpecArgs(
                selector=dict(LABELS),
                ports=[k8s.core.v1.ServicePortArgs(name='http', port=PORTAL.port, target_port='http', protocol='TCP')],
            ),
            opts=self.child_opts(),
        )
        self.grant = crds.gateway.v1.ReferenceGrant(
            f'{name}-gate',
            metadata=k8s.meta.v1.ObjectMetaArgs(name='gate', namespace=namespace),
            spec=crds.gateway.v1.ReferenceGrantSpecArgs(
                from_=[
                    crds.gateway.v1.ReferenceGrantSpecFromArgs(
                        group='gateway.networking.k8s.io', kind='HTTPRoute', namespace=gated_namespace
                    )
                    for gated_namespace in sorted({*gated, CANARY_NAMESPACE})
                ],
                to=[crds.gateway.v1.ReferenceGrantSpecToArgs(group='', kind='Service', name=PORTAL.service)],
            ),
            opts=self.child_opts(),
        )

        self.redis = k8s.apps.v1.Deployment(
            f'{name}-redis',
            metadata=k8s.meta.v1.ObjectMetaArgs(
                name=_REDIS, namespace=namespace, annotations={'reloader.stakater.com/auto': 'true'}
            ),
            spec=k8s.apps.v1.DeploymentSpecArgs(
                replicas=1,
                selector=k8s.meta.v1.LabelSelectorArgs(match_labels=dict(_REDIS_LABELS)),
                template=k8s.core.v1.PodTemplateSpecArgs(
                    metadata=k8s.meta.v1.ObjectMetaArgs(labels=dict(_REDIS_LABELS)),
                    spec=_redis(redis_image, self.secrets.metadata.name, self.constraints),
                ),
            ),
            opts=self.child_opts(),
        )
        self.redis_service = k8s.core.v1.Service(
            f'{name}-redis',
            metadata=k8s.meta.v1.ObjectMetaArgs(name=_REDIS, namespace=namespace),
            spec=k8s.core.v1.ServiceSpecArgs(
                selector=dict(_REDIS_LABELS),
                ports=[k8s.core.v1.ServicePortArgs(name='redis', port=_REDIS_PORT, target_port='redis')],
            ),
            opts=self.child_opts(),
        )

        self.target = k8s.apps.v1.Deployment(
            f'{name}-canary-target',
            metadata=k8s.meta.v1.ObjectMetaArgs(name=_TARGET, namespace=canary_namespace),
            spec=k8s.apps.v1.DeploymentSpecArgs(
                replicas=1,
                selector=k8s.meta.v1.LabelSelectorArgs(match_labels=dict(_TARGET_LABELS)),
                template=k8s.core.v1.PodTemplateSpecArgs(
                    metadata=k8s.meta.v1.ObjectMetaArgs(labels=dict(_TARGET_LABELS)),
                    spec=k8s.core.v1.PodSpecArgs(
                        automount_service_account_token=False,
                        enable_service_links=False,
                        **self.constraints.pod_fields(),
                        security_context=k8s.core.v1.PodSecurityContextArgs(
                            run_as_non_root=True,
                            seccomp_profile=k8s.core.v1.SeccompProfileArgs(type='RuntimeDefault'),
                        ),
                        containers=[_target(target_image)],
                        volumes=[k8s.core.v1.VolumeArgs(name='tmp', empty_dir=k8s.core.v1.EmptyDirVolumeSourceArgs())],
                    ),
                ),
            ),
            opts=self.child_opts(),
        )
        self.target_service = k8s.core.v1.Service(
            f'{name}-canary-target',
            metadata=k8s.meta.v1.ObjectMetaArgs(name=_TARGET, namespace=canary_namespace),
            spec=k8s.core.v1.ServiceSpecArgs(
                selector=dict(_TARGET_LABELS),
                ports=[k8s.core.v1.ServicePortArgs(name='http', port=_TARGET_PORT, target_port='http')],
            ),
            opts=self.child_opts(),
        )
        self.canary_route = crds.gateway.v1.HTTPRoute(
            f'{name}-canary',
            metadata=k8s.meta.v1.ObjectMetaArgs(name=AUTH_CANARY.cronjob, namespace=canary_namespace),
            spec=crds.gateway.v1.HTTPRouteSpecArgs(
                parent_refs=[
                    crds.gateway.v1.HTTPRouteSpecParentRefsArgs(
                        name=conventions.GATEWAY_INTERNET, namespace=conventions.GATEWAY_NAMESPACE
                    )
                ],
                hostnames=[_portal_host(exposure)],
                rules=[
                    crds.gateway.v1.HTTPRouteSpecRulesArgs(
                        matches=[
                            crds.gateway.v1.HTTPRouteSpecRulesMatchesArgs(
                                path=crds.gateway.v1.HTTPRouteSpecRulesMatchesPathArgs(
                                    type='PathPrefix', value=CANARY_PATH
                                )
                            )
                        ],
                        filters=[gate_filter(Gate.SESSION)],
                        backend_refs=[
                            crds.gateway.v1.HTTPRouteSpecRulesBackendRefsArgs(
                                name=self.target_service.metadata.name, port=_TARGET_PORT
                            )
                        ],
                    )
                ],
            ),
            opts=self.child_opts(),
        )
        self.canary_run = k8s.core.v1.ConfigMap(
            f'{name}-canary',
            metadata=k8s.meta.v1.ObjectMetaArgs(name=AUTH_CANARY.cronjob, namespace=canary_namespace),
            data={_CANARY_SCRIPT: templates.load('kluster.components.apps.authelia', f'templates/{_CANARY_SCRIPT}')},
            opts=self.child_opts(),
        )
        starting = AUTH_CANARY.starting_deadline
        self.canary = k8s.batch.v1.CronJob(
            f'{name}-canary',
            metadata=k8s.meta.v1.ObjectMetaArgs(name=AUTH_CANARY.cronjob, namespace=canary_namespace),
            spec=k8s.batch.v1.CronJobSpecArgs(
                schedule=_CANARY_SCHEDULE,
                concurrency_policy='Forbid',
                starting_deadline_seconds=int(starting.total_seconds()) if starting is not None else None,
                successful_jobs_history_limit=1,
                failed_jobs_history_limit=3,
                job_template=_canary_job(
                    canary_image, portal=portal, address=fronts.internet.balancer_v4, constraints=self.constraints
                ),
            ),
            opts=self.child_opts(depends_on=[self.canary_run]),
        )
        self.canary_rules = operator_object(
            f'{name}-canary',
            kind='VMRule',
            metadata=k8s.meta.v1.ObjectMetaArgs(name=AUTH_CANARY.cronjob, namespace=canary_namespace),
            spec={'groups': [rule_group_spec(RuleGroup(AUTH_CANARY.cronjob, scheduled_job_rules(AUTH_CANARY)))]},
            opts=self.child_opts(),
        )

        self.exposed = expose(
            name,
            exposure,
            namespace=namespace,
            backend=Backend(service=self.service.metadata.name, port=PORTAL.port),
            selector=None,
            fronts=fronts,
            zone_ids=zone_ids,
            opts=self.child_opts(),
        )

        self.register_outputs({})
