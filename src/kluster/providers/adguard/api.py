"""The transport, and the one verdict per instance that every request waits on.

**The transport.** Every request goes to `<endpoint>/control/<path>`, and a
credentialed one carries the login as HTTP basic authentication, which the
instance accepts on every `/control/` path. The login goes out as UTF-8,
because that is how the instance stored it: `configure` took it as JSON, while
`requests` writes a `str` login in Latin-1, which no account made from a
non-ASCII login matches. A request the instance refuses
raises `requests.HTTPError` naming the method, the path, the status and the
instance's own reason, which it writes as the body of every refusal: a refusal
is never read as an empty answer. **A redirect is a refusal too**, naming where
it points, and is never followed: an instance in first run answers every path
but its install routes with `302` to an `install.html` beside the path, and
following those ends in `TooManyRedirects` thirty round trips later, saying
nothing about why.

**A body goes out as JSON whose whole numbers are integers.** A property bag
reaches a dynamic provider with every number a float, and the instance decodes
its integer fields with Go's `encoding/json`, which refuses `24.0` for a
`uint32`. So `wire` writes a float that holds a whole number as an integer.

**The classification** says what an instance is before any kind sends it the
login, from at most three requests:

1.  `GET <endpoint>/control/status` with no credentials, which the instance's
    login limiter never counts. `401` means an account exists: step 3. `200`
    means the instance holds no account. A refused or timed-out connection
    goes to step 2. Any other answer is unusable, and named.
2.  `GET <setup_endpoint>/control/install/get_addresses` with no credentials.
    `200` means **first run**: the instance started with no configuration
    file, answers no DNS, and serves only its setup wizard, there. A refused
    or timed-out connection here as well means the instance is unreachable.
3.  `GET <endpoint>/control/status` with the login. `200` means
    **configured**; `401` means the login is refused.

The API's port is asked first, so a configured instance never sees a request on
the setup port, and a caller that reaches the API's port alone waits on the
other only when the API is down.

**The verdict is held for the life of the plugin process**, one per
`(endpoint, credential fingerprint)`. A process serves one phase of a command,
up to four operations at once: a `pulumi up` runs its preview and its update in
two, and under `--refresh` each phase runs its refresh in a process of its own.
So the classification sends the login once per instance per process, however
many resources the instance has and whatever order the engine reads them in,
and a kind's later requests carry it only to an instance found configured with
it. An unusable verdict -- refused, accountless, unreachable, or a
classification whose request failed some other way -- raises again from the
memo, sending nothing. A first-run verdict becomes "configured" once the setup
has configured the instance (`configured`).

**A refused login costs one failed login per instance per command**, across
those processes, against an instance that blocks the caller's address for
fifteen minutes after five. Planning sends no login, since `check` and `diff`
call no instance, and a refresh that is refused fails its phase and so the
command. The login a phase carries is not always the configured one: the
`pulumi-python` provider records the stack's configuration in state at every
update, failed ones included, and a refresh is served with the recorded one. So
a refreshing command sends the recorded login in its refreshes and the current
one only in its update, which runs only if those refreshes were accepted; and
after a wrong login has been recorded, a refreshing command keeps sending it
whatever the configuration now says, until a plain `pulumi up` records the
current one.

**Why a lock rather than a library**: the guarded section is one bounded
exchange, and what it has to guarantee is single flight -- that concurrent
callers asking about one instance share one exchange. The standard library has
no single-flight memo, and `functools.cache` computes once per concurrent
caller. One module-level lock is held across the whole classification of one
instance, the shape the plugin host guards its own provider cache in.
"""

from __future__ import annotations

import enum
import threading
from collections.abc import Mapping, Sequence
from typing import Any, Literal

import requests
from requests.auth import HTTPBasicAuth

from kluster.providers.configured import fingerprint

__all__ = (
    'DOWNLOAD_TIMEOUT',
    'TIMEOUT',
    'UNANSWERED',
    'Api',
    'Unusable',
    'Verdict',
    'classify',
    'configured',
    'described',
    'wire',
)

#: Long enough for a busy resolver, short enough that an unreachable UDM fails
#: the resource instead of hanging the stack.
TIMEOUT = 15

#: For a request that makes the instance download a filter list before it
#: answers. The instance bounds its own download by its HTTP client's timeout,
#: which is its server's five-minute write timeout (`home/httpclient.go`,
#: `home/web.go` `writeTimeout`), so the request is given that and a margin.
DOWNLOAD_TIMEOUT = 330


class Verdict(enum.Enum):
    """What an instance is, where it can be configured at all."""

    #: It holds an account, and the login is it.
    CONFIGURED = 'configured'
    #: It started with no configuration file and serves only its setup wizard.
    FIRST_RUN = 'first run'


class Unusable(RuntimeError):
    """The instance cannot be configured with this login: refused, holding no account, or unreachable."""


def wire(value: Any) -> Any:
    """`value` with every float that holds a whole number written as an integer, at any depth."""
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, Mapping):
        return {key: wire(item) for key, item in value.items()}  # pyright: ignore[reportUnknownVariableType]
    if isinstance(value, Sequence) and not isinstance(value, str):
        return [wire(item) for item in value]  # pyright: ignore[reportUnknownVariableType]
    return value


def described(response: requests.Response) -> str:
    """A response as a message names it: the status, where a redirect points, and the instance's reason."""
    where = f' to {response.headers.get("Location")}' if 300 <= response.status_code < 400 else ''
    reason = response.text.strip()
    return f'{response.status_code}{where}' + (f': {reason}' if reason else '')


class Api:
    """One session onto one instance, with the login or with no credentials at all."""

    def __init__(self, endpoint: str, username: str | None = None, password: str | None = None) -> None:
        self.base: str = endpoint.rstrip('/')
        self.session: requests.Session = requests.Session()
        if username is not None and password is not None:
            self.session.auth = HTTPBasicAuth(username.encode(), password.encode())

    def answer(
        self, method: Literal['GET', 'POST', 'PUT'], path: str, body: object = None, *, timeout: int = TIMEOUT
    ) -> requests.Response:
        """The instance's response, whatever its status. A redirect is returned, not followed."""
        url = self._url(path)
        match method:
            case 'GET':
                return self.session.get(url, timeout=timeout, allow_redirects=False)
            case 'POST':
                return self.session.post(url, json=wire(body), timeout=timeout, allow_redirects=False)
            case 'PUT':
                return self.session.put(url, json=wire(body), timeout=timeout, allow_redirects=False)

    def get(self, path: str) -> Any:
        """The instance's answer to a GET, decoded."""
        response = self.answer('GET', path)
        _refused(response, 'GET', path)
        return response.json()

    def post(self, path: str, body: object, *, timeout: int = TIMEOUT) -> None:
        _refused(self.answer('POST', path, body, timeout=timeout), 'POST', path)

    def put(self, path: str, body: object) -> None:
        _refused(self.answer('PUT', path, body), 'PUT', path)

    def _url(self, path: str) -> str:
        return f'{self.base}/control/{path}'


def _refused(response: requests.Response, method: str, path: str) -> None:
    if response.status_code >= 300:
        raise requests.HTTPError(f'{method} /control/{path} answered {described(response)}')


#: Every verdict this process reached, by `(endpoint, credential fingerprint)`:
#: a `Verdict`, or the reason an unusable instance is refused with.
_verdicts: dict[tuple[str, str], Verdict | str] = {}
_lock = threading.Lock()

#: A connection that never produced an answer: refused, reset, or timed out.
UNANSWERED = (requests.ConnectionError, requests.Timeout)


def classify(endpoint: str, setup_endpoint: str, username: str, password: str) -> Verdict:
    """What the instance at `endpoint` is to this login; `Unusable` where it is neither configured nor in first run."""
    key = _key(endpoint, username, password)
    with _lock:
        held = _verdicts.get(key)
        if held is None:
            try:
                held = _classified(endpoint.rstrip('/'), setup_endpoint.rstrip('/'), username, password)
            except requests.RequestException as broken:
                # An answer can still break off after the login went out, so
                # this too is a verdict, or every resource would send it again.
                held = f'classifying {endpoint} failed: {type(broken).__name__}: {broken}'
            _verdicts[key] = held
    if isinstance(held, str):
        raise Unusable(held)
    return held


def configured(endpoint: str, username: str, password: str) -> None:
    """Record that the instance at `endpoint` now holds this login as its account."""
    with _lock:
        _verdicts[_key(endpoint, username, password)] = Verdict.CONFIGURED


def _key(endpoint: str, username: str, password: str) -> tuple[str, str]:
    return endpoint.rstrip('/'), fingerprint(f'{username}:{password}')


def _classified(endpoint: str, setup_endpoint: str, username: str, password: str) -> Verdict | str:
    try:
        anonymous = Api(endpoint).answer('GET', 'status')
    except UNANSWERED as down:
        return _first_run_or_down(endpoint, setup_endpoint, down)
    if anonymous.status_code == 200:
        return (
            f'{endpoint} answers without a login: the instance holds no account, so anyone who reaches it can '
            'configure it. Only the first-run setup makes an account; reset the instance (move its '
            'AdGuardHome.yaml aside and restart its machine) and run again with --refresh.'
        )
    if anonymous.status_code != 401:
        return (
            f'GET {endpoint}/control/status answered {described(anonymous)}, which is neither an instance with an '
            'account (401) nor one without (200)'
        )
    try:
        answered = Api(endpoint, username, password).answer('GET', 'status')
    except UNANSWERED as down:
        return f'{endpoint} stopped answering while it was being classified: {down}'
    if answered.status_code == 200:
        return Verdict.CONFIGURED
    if answered.status_code == 401:
        return (
            f'{endpoint} refused the login in adguardUsername and adguardPassword (401). Either it is not the '
            "instance's account, or the instance has blocked this address for 15 minutes after five refused "
            'logins; the answer does not tell the two apart. A run sends the login once per instance, so five '
            'runs within 15 minutes block the address too. A refresh sends the login the last pulumi up '
            'recorded, so once the configuration is right a plain pulumi up comes first. No endpoint changes an '
            'account: a rotated or lost login takes a reset of the instance (move its AdGuardHome.yaml aside and '
            'restart its machine).'
        )
    return f'GET {endpoint}/control/status with the login answered {described(answered)}'


def _first_run_or_down(endpoint: str, setup_endpoint: str, down: Exception) -> Verdict | str:
    try:
        wizard = Api(setup_endpoint).answer('GET', 'install/get_addresses')
    except UNANSWERED as also:
        return f'the instance is unreachable: {endpoint} ({down}) and its setup at {setup_endpoint} ({also})'
    if wizard.status_code == 200:
        return Verdict.FIRST_RUN
    return (
        f'{endpoint} is unreachable ({down}), and {setup_endpoint}/control/install/get_addresses answered '
        f'{described(wizard)}, which is not an instance in first run'
    )
