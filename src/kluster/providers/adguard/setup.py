"""`AdGuardSetup`: what only an instance's first run sets -- its account and its listen addresses.

An instance that starts with no configuration file is in **first run**: it
answers no DNS, serves its setup wizard alone, at `setup_endpoint`, and holds
no account. `POST /control/install/configure` is what leaves it, and the only
endpoint that makes an account: it adds the one account, starts the DNS
server, writes the file, and only then moves the web server to the requested
address. Once the file exists the install routes are gone, so nothing changes
an account or a listen address afterwards, and an instance returns to first
run only by starting with its file moved aside -- a reset.

**What each operation does with the classification** (`api.classify`):

-   **`create`** configures an instance in first run with `listen` and the
    login, sent as JSON with no credentials, and then waits until `endpoint`
    answers the login, since the instance moves its web server only after it
    has answered. An instance already configured with the login is adopted,
    and nothing is written. A `500` from `configure` comes after the account
    was added: the instance is **half-applied**, its DNS server answering on
    its defaults with no account and its wizard still up, refusing every later
    `configure` until its machine restarts, so it raises saying so and is never
    retried. A refusal (`400`, `422`) raises with the instance's reason. The
    classification cannot tell a half-applied instance from one in first run,
    since both refuse the API and answer on the setup port, so the message for
    a refusal names both: a first-run instance has applied nothing, and a
    half-applied one, which refuses with `address already in use`, needs its
    machine restarted.
-   **`read`** reports an instance in first run gone, since it holds no
    configuration, so a refreshing run re-creates this resource and the kinds
    that depend on it. A configured instance gives back the stored bag:
    `listen` is not read back, because `GET /control/status` reports the
    expanded interface list rather than the bind address.
-   **`update`** raises on a changed `listen`, which only first run sets. A
    moved login classifies the instance again with it, one credentialed
    request; a refusal raises. A moved `endpoint` or `setup_endpoint` alone is
    recorded with no call.
-   **`delete`** calls nothing: no endpoint returns an instance to first run.

Every other verdict raises in every operation. A rotated or lost login is
therefore a reset of the instance, after which the refreshing run configures
it again.

**`check` refuses offline** what `configure` would refuse, and what it would
accept but leave unreachable: a port 0, web and DNS on one port, an address
that does not parse, an `endpoint` other than `listen.web`, an empty username
or one holding a colon, which basic authentication cannot carry, and a password
under `MIN_PASSWORD` characters. The login is named by its configuration key
and never by its value.
"""

from __future__ import annotations

import ipaddress
import logging
import time
from collections.abc import Mapping
from typing import Any, TypedDict, cast, final
from urllib.parse import urlsplit

import pulumi
import pulumi.dynamic as dynamic

from kluster.providers.adguard.api import UNANSWERED, Api, Unusable, Verdict, configured, described
from kluster.providers.adguard.base import (
    ENDPOINT,
    INSTANCE,
    PASSWORD_CONFIG,
    RECORDED,
    SETUP_ENDPOINT,
    USERNAME_CONFIG,
    InstanceProvider,
    SetupNotRun,
    gone,
    missing_or_extra,
)
from kluster.providers.configured import SESSION, is_unknown

__all__ = (
    'LISTEN',
    'MIN_PASSWORD',
    'AdGuardSetup',
    'AdGuardSetupProvider',
    'Address',
    'HalfApplied',
    'Listen',
    'ListenChanged',
    'SetupRefused',
)

log = logging.getLogger(__name__)

#: The addresses `configure` binds the web server and the DNS server to.
LISTEN = 'listen'

#: The shortest password `configure` accepts, counted in characters
#: (`PasswordMinRunes`, `home/controlinstall.go`).
MIN_PASSWORD = 8

#: How long the instance has, after `configure` answered, to answer the login
#: at `endpoint`. A stop-loss: the instance rebinds within milliseconds.
REBIND_TIMEOUT = 30

#: The pause between two attempts to reach `endpoint` after `configure`.
POLL_INTERVAL = 0.5


class Address(TypedDict):
    ip: str
    port: int


class Listen(TypedDict):
    """`configure`'s own shape: where the web server and the DNS server listen."""

    web: Address
    dns: Address


_PARTS = frozenset({'web', 'dns'})
_KEYS = Address.__required_keys__


class SetupRefused(RuntimeError):
    """The instance refused `configure`: one in first run applied nothing, a half-applied one needs a restart."""


class HalfApplied(RuntimeError):
    """`configure` failed after adding the account: the instance refuses every `configure` until a restart."""


class ListenChanged(RuntimeError):
    """A declared listen address moved, which only first run sets."""


def _listen(value: Any) -> dict[str, dict[str, Any]]:
    """`listen` as `configure` takes it, every port an integer."""
    held = cast('Mapping[str, Mapping[str, Any]]', value or {})
    return {
        part: {'ip': str(held.get(part, {}).get('ip')), 'port': int(held.get(part, {}).get('port') or 0)}
        for part in sorted(_PARTS)
    }


def _login(props: Mapping[str, Any]) -> str:
    """The credential half of a session stamp: what moves on a rotation and not on a re-address."""
    return str(props.get(SESSION, '')).rpartition('#')[2]


@final
class AdGuardSetupProvider(InstanceProvider):
    kind = 'setup'
    sections = (LISTEN,)

    def check(self, olds: dict[str, Any], news: dict[str, Any]) -> dynamic.CheckResult:
        result = super().check(olds, news)
        failures = list(result.failures or [])
        if not self.username:
            failures.append(dynamic.CheckFailure(USERNAME_CONFIG, f'{USERNAME_CONFIG} is empty'))
        if ':' in self.username:
            # Basic authentication ends the username at its first colon, so
            # configure would make an account no request can name.
            failures.append(
                dynamic.CheckFailure(
                    USERNAME_CONFIG, f'{USERNAME_CONFIG} holds a colon, which basic authentication cannot carry'
                )
            )
        if len(self.password) < MIN_PASSWORD:
            failures.append(
                dynamic.CheckFailure(
                    PASSWORD_CONFIG,
                    f'{PASSWORD_CONFIG} is shorter than {MIN_PASSWORD} characters, which configure refuses',
                )
            )
        return dynamic.CheckResult(result.inputs, failures)

    def _refusals(self, news: Mapping[str, Any], failures: list[dynamic.CheckFailure]) -> None:
        listen = news.get(LISTEN)
        if not missing_or_extra(LISTEN, listen, _PARTS, failures):
            return
        parts = cast('Mapping[str, Any]', listen)
        if not all(missing_or_extra(LISTEN, parts[part], _KEYS, failures, label=f'listen.{part}') for part in _PARTS):
            return
        for part in sorted(_PARTS):
            address = cast('Mapping[str, Any]', parts[part])
            try:
                _ = ipaddress.ip_address(str(address['ip']))
            except ValueError:
                failures.append(dynamic.CheckFailure(LISTEN, f'listen.{part}.ip {address["ip"]!r} is not an address'))
            port = address['port']
            if not isinstance(port, int | float) or isinstance(port, bool) or not 0 < port < 65536 or port % 1:
                failures.append(
                    dynamic.CheckFailure(LISTEN, f'listen.{part}.port {port!r} is not a port from 1 to 65535')
                )
        if not failures and parts['web']['port'] == parts['dns']['port']:
            failures.append(dynamic.CheckFailure(LISTEN, 'listen.web and listen.dns are on one port'))
        endpoint = news.get(ENDPOINT)
        if not failures and not is_unknown(endpoint):
            self._reaches_web(str(endpoint), _listen(listen)['web'], failures)

    def _reaches_web(self, endpoint: str, web: Mapping[str, Any], failures: list[dynamic.CheckFailure]) -> None:
        """Record a failure where `endpoint` is not where `configure` puts the web server."""
        parts = urlsplit(endpoint)
        try:
            port = parts.port or {'http': 80, 'https': 443}.get(parts.scheme)
        except ValueError:
            port = None
        if port != web['port']:
            failures.append(
                dynamic.CheckFailure(ENDPOINT, f'endpoint {endpoint} is not on listen.web.port {web["port"]}')
            )
        bound = ipaddress.ip_address(web['ip'])
        if bound.is_unspecified:
            return
        try:
            same = ipaddress.ip_address(parts.hostname or '') == bound
        except ValueError:
            same = False
        if not same:
            failures.append(dynamic.CheckFailure(ENDPOINT, f'endpoint {endpoint} is not on listen.web.ip {bound}'))

    def _comparable(self, section: str, value: Any) -> object:
        return _listen(value)

    def create(self, props: dict[str, Any]) -> dynamic.CreateResult:
        if self._verdict(props) is Verdict.FIRST_RUN:
            self._configure(props)
        else:
            log.info('%s is configured with the login already: adopted, nothing written', props[INSTANCE])
        return dynamic.CreateResult(id_=f'{props[INSTANCE]}|{self.kind}', outs=props)

    def read(self, id_: str, props: dict[str, Any]) -> dynamic.ReadResult:
        if self._verdict(props) is Verdict.FIRST_RUN:
            return gone()
        stored = {key: props[key] for key in (*RECORDED, LISTEN) if key in props}
        # Two bags, because the provider host writes its own key into each.
        return dynamic.ReadResult(id_=id_, outs=dict(stored), inputs=dict(stored))

    def update(self, _id: str, olds: dict[str, Any], news: dict[str, Any]) -> dynamic.UpdateResult:
        if self._changed(olds, news):
            raise ListenChanged(
                f'{news[INSTANCE]}: listen moved from {_listen(olds.get(LISTEN))} to {_listen(news[LISTEN])}. Only '
                "an instance's first run sets it: reset the instance (move its AdGuardHome.yaml aside and restart "
                'its machine), then run again with --refresh.'
            )
        if _login(olds) != _login(news) and self._verdict(news) is Verdict.FIRST_RUN:
            raise SetupNotRun(
                f'{news[INSTANCE]} is in first run, so there is no account to move to the new login: run again '
                'with --refresh, which re-creates its setup.'
            )
        return dynamic.UpdateResult(outs=news)

    def _configure(self, props: Mapping[str, Any]) -> None:
        instance, setup = props[INSTANCE], str(props[SETUP_ENDPOINT]).rstrip('/')
        listen = _listen(props[LISTEN])
        log.info('configuring %s through its first-run setup at %s, listening on %s', instance, setup, listen)
        body = {**listen, 'username': self.username, 'password': self.password}
        response = Api(setup).answer('POST', 'install/configure', body)
        if response.status_code in (400, 422):
            raise SetupRefused(
                f'{instance}: POST {setup}/control/install/configure answered {described(response)}. An instance '
                'in first run that refuses configure has applied nothing. One whose earlier configure answered 500 '
                'is half-applied instead: it answers DNS on its defaults with no account, and refuses every '
                'configure, with "address already in use", until its machine restarts. If an earlier run reported '
                'that 500, restart the machine or container that runs the instance, then run again.'
            )
        if response.status_code == 500:
            raise HalfApplied(
                f'{instance}: POST {setup}/control/install/configure answered {described(response)}. The '
                'instance added the account and started its DNS server before it failed, so it is half-applied: '
                'it refuses every configure until its machine restarts, and comes back in first run. Restart the '
                'machine or container that runs the instance, then run again.'
            )
        if response.status_code != 200:
            raise SetupRefused(f'{instance}: POST {setup}/control/install/configure answered {described(response)}')
        self._await_login(props)
        configured(self._endpoint(props), self.username, self.password)
        log.info('%s answers the login at %s: configured', instance, self._endpoint(props))

    def _await_login(self, props: Mapping[str, Any]) -> None:
        """Wait until `endpoint` answers the login, which it does once the instance has moved its web server there.

        Only a connection that produced no answer is retried. Any answer is
        final, `401` above all, since every refused login counts toward the
        instance's limiter.
        """
        endpoint = self._endpoint(props)
        log.info(
            'waiting for %s to answer the login: the instance moves its web server there after it answers '
            'configure, usually within milliseconds; giving up after %ds',
            endpoint,
            REBIND_TIMEOUT,
        )
        session = Api(endpoint, self.username, self.password)
        deadline = time.monotonic() + REBIND_TIMEOUT
        while True:
            try:
                answered = session.answer('GET', 'status')
            except UNANSWERED as exc:
                if time.monotonic() + POLL_INTERVAL > deadline:
                    raise Unusable(
                        f'{props[INSTANCE]} took its configure but {endpoint} did not answer within '
                        f'{REBIND_TIMEOUT}s; last: {exc}'
                    ) from exc
                log.info('not yet: %s', exc)
                time.sleep(POLL_INTERVAL)
                continue
            if answered.status_code != 200:
                raise Unusable(
                    f'{props[INSTANCE]} took its configure, but GET {endpoint}/control/status with the login '
                    f'answered {described(answered)}'
                )
            return


@final
class AdGuardSetup(dynamic.Resource, module='adguard', name='AdGuardSetup'):
    """One instance's first-run setup: its account, from the configured login, and its listen addresses."""

    instance: pulumi.Output[str]
    endpoint: pulumi.Output[str]
    setup_endpoint: pulumi.Output[str]
    listen: pulumi.Output[dict[str, Any]]

    def __init__(
        self,
        name: str,
        *,
        instance: pulumi.Input[str],
        endpoint: pulumi.Input[str],
        setup_endpoint: pulumi.Input[str],
        listen: pulumi.Input[Listen],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        """Declare that `instance` was set up with the configured login, listening on `listen`.

        `endpoint` is where its API answers once set up, which `listen.web`
        has to put it on; `setup_endpoint` is where its wizard answers in first
        run. Both may move without the instance changing.
        """
        super().__init__(
            AdGuardSetupProvider(),
            name,
            {INSTANCE: instance, ENDPOINT: endpoint, SETUP_ENDPOINT: setup_endpoint, LISTEN: listen},
            opts,
        )
