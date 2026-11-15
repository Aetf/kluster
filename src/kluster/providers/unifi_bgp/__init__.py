"""The site's BGP configuration on the UniFi controller (physical/gateway.md §1.3).

The controller holds one BGP configuration per site, the text the console's
Settings → Routing → BGP page uploads: FRR's own configuration language, with
its route maps and prefix lists after the `router bgp` block. It is one object
on the Network v2 API, `v2/api/site/<site>/bgp/config`. A GET answers a list,
empty on a site that holds none and holding the one object otherwise; a POST
of the whole object writes it, on a site that holds one as on a site that
holds none; a DELETE takes it away. The object carries the text as
`frr_bgpd_config`, and beside it `enabled`, `description` and
`uploaded_file_name`. The API key reads the text back whole. When the object is
enabled, the firmware runs the routing suite as the controller's own children;
when it is gone, the suite stops.

**This package is one resource: the site's configuration, held whole.** Its
identity is the site. What it declares is the text, the description and the
file name the controller stores beside it. It has no `enabled` input: a
declared configuration is an enabled one, and its absence is the resource's
delete.

-   **`create` reads first.** A site that holds no configuration is sent one
    POST of the whole object. A site that already holds exactly what is
    declared, switched on, is taken as created, so a run that failed after its
    write can be run again. Any other configuration is refused by name and left
    as it is: it is deleted in the console, and this resource never adopts it.
-   **`create` and `update` read the object back after the write** and fail
    unless it is what was sent.
-   **`diff` reads the controller.** A console edit, or the page switched off,
    is in the next preview without a refresh, and `update` writes the declared
    object back.
-   **`delete` sends the DELETE**, and reads back that the site holds none. A
    configuration already gone is success.

**Every replacement is delete-first.** A site holds one configuration, and
`create` takes an identical one as created with no write of its own, so it
has no identity to record and `delete` removes whatever the site holds. A
replacement created first would have its closing delete take away the
configuration its create had just taken as its own, in a run that reports
success (style/pulumi.md, on a target that adopts). The resource therefore
declares `delete_before_replace` itself, and the provider's own plan for a
change of site agrees. The session is down from the delete to the create.

**No message, log line or stamp carries the text**, which holds the session's
password. Drift is reported as the fact that a field differs, and the
controller's reason for refusing a write is relayed with every word of the
declared text taken out of it, since a parser's complaint quotes the line it
refused. The text is a secret on the resource, in its inputs and in its
outputs alike.

**The credential is the controller's API key**, read in `configure`, under the
key `API_KEY_CONFIG` names. Where the session goes and whether it verifies the
controller's certificate are declared inputs, since a provider decides neither
(framework/pulumi.md §5.2). `check` stamps `session` and `provider_version`
through `kluster.providers.configured`.

**The only update that sends nothing is a stamp's, or an address's.** A moved
stamp or a moved address changes nothing about the configuration, so an update
reached for one of those alone records the stamps and calls no endpoint. An
update reached with every declared input equal was planned by `diff` for drift,
and it writes. A run in which a stamp moved together with a console edit
repairs the edit on the run after it, whose `diff` reads it again.

**This package is a layer that comes out whole**, with the one construction
that builds its resource, `components/gateway/unifi.py`'s `site_bgp`, when the
pinned `filipowm/unifi` provider carries a BGP resource that round-trips the
declared text with the stack's key. The tripwire is the SDK regeneration on a
change of that pin (declarative/physical.md §6). The swap leaves this resource
out of state without its delete: declared in the same apply as the bridged
resource, the bridged create would run first and this resource's delete would
then remove what it wrote. So the change that declares the bridged resource
is applied after `pulumi state delete` of this one, which takes it out of
state and sends the controller nothing, and the bridged resource then finds
the configuration it declares already on the site.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Annotated, Any, cast, final

import pulumi
import pulumi.dynamic as dynamic
import requests
import urllib3
from urllib3.exceptions import InsecureRequestWarning

from kluster.lib.secret import Secret
from kluster.providers.configured import STAMPS, ConfiguredProvider, declared_change, has_unknowns

__all__ = (
    'API_KEY_CONFIG',
    'COMPARED',
    'CONTENT',
    'DECLARED',
    'ENABLED',
    'IDENTITY',
    'TIMEOUT',
    'VERSION',
    'BgpConfiguration',
    'BgpConfigurationProvider',
    'ConfigurationNotStored',
    'ForeignConfiguration',
    'WriteRefused',
)

#: Long enough for a controller under load, short enough that an unreachable
#: gateway fails the resource instead of hanging the stack. Nothing is retried:
#: a re-run is safe, `create` taking an identical object as created.
TIMEOUT = 15

#: The stack-configuration key the controller's API key is read from
#: (`configured`).
API_KEY_CONFIG = 'unifiApiKey'

#: The header the controller reads an API key from.
API_KEY_HEADER = 'X-API-KEY'

#: What identifies a configuration, and therefore what replaces one: the site.
IDENTITY = ('site',)

#: What the configuration is: the text, and what the controller stores beside
#: it.
CONTENT = ('config', 'description', 'file_name')

#: How this run reaches the controller. A change to one of them moves the
#: session stamp, and nothing about the configuration.
TRANSPORT = ('api_url', 'allow_insecure')

#: What a caller declares.
DECLARED = (*IDENTITY, *CONTENT, *TRANSPORT)

#: What `diff` compares, and the whole of it -- the declared properties plus
#: the two `check` stamps.
COMPARED = (*DECLARED, *STAMPS)

#: The output that records whether the controller held the configuration
#: switched on: true after every write, and what `read` found otherwise.
ENABLED = 'enabled'

#: This module's version, bumped by hand when an operation's behavior changes
#: (`configured`).
VERSION = '1'

#: The declared fields under the names the controller stores them by.
_FIELDS = {'config': 'frr_bgpd_config', 'description': 'description', 'file_name': 'uploaded_file_name'}

#: How a field is named in a message, which never carries its value.
_SPOKEN = {'config': 'the configuration text', 'description': 'the description', 'file_name': 'the file name'}

#: The fields of a refusal the controller's v2 API answers with that are
#: relayed: its error code, and its sentence.
_REASON_FIELDS = ('code', 'message')

#: What stands in a relayed reason for a word of the declared text.
_REDACTED = '[declared]'

#: The shortest word of the declared text taken out of a reason wherever it
#: stands (`_scrubbed`).
_SHORTEST = 4


class WriteRefused(RuntimeError):
    """The controller refused a write, with its own reason, the declared text taken out of it."""


class ForeignConfiguration(RuntimeError):
    """The site holds a configuration other than the declared one, named with how it differs."""


class ConfigurationNotStored(RuntimeError):
    """The controller holds something other than what was just written."""


def _base(props: Mapping[str, Any]) -> str:
    """The controller, without the trailing slash a caller may write."""
    return str(props['api_url']).rstrip('/')


def _url(props: Mapping[str, Any]) -> str:
    return f'{_base(props)}/proxy/network/v2/api/site/{props["site"]}/bgp/config'


def _object(props: Mapping[str, Any]) -> dict[str, Any]:
    """What the controller is sent: the whole object, switched on."""
    return {'enabled': True, **{stored: str(props[declared]) for declared, stored in _FIELDS.items()}}


def _differences(held: Mapping[str, Any], props: Mapping[str, Any]) -> list[str]:
    """How the held object departs from the declared one, one phrase each, naming fields and never their values."""
    differing = [_SPOKEN[declared] for declared, stored in _FIELDS.items() if held.get(stored) != props[declared]]
    switched_off = [] if held.get('enabled') is True else ['it is switched off']
    return [*(f'{field} differs' for field in differing), *switched_off]


def _named(held: Mapping[str, Any]) -> str:
    """A configuration as a message names it: its id, its description and its file name."""
    return f'{held.get("_id")!s} ({held.get("description")!r}, file {held.get("uploaded_file_name")!r})'


def _scrubbed(said: str, text: str) -> str:
    """`said` with every word of `text` taken out.

    A word of `_SHORTEST` characters or more goes wherever it stands, inside
    quotes or another word included, longest first so that no part of a word
    survives a shorter one; a shorter word goes where it stands alone, so that
    the controller's own words keep their letters.
    """
    for word in sorted(set(text.split()), key=len, reverse=True):
        if len(word) >= _SHORTEST:
            said = said.replace(word, _REDACTED)
        else:
            said = re.sub(rf'(?<!\S){re.escape(word)}(?!\S)', _REDACTED, said)
    return said


def _reason(response: requests.Response, text: str) -> str:
    """The controller's reason for a refusal, as a message may carry it.

    The fields of a v2 refusal that say what was refused, and only those: a
    parser's complaint quotes the line it refused, so whatever the controller
    says is relayed with the declared text taken out of it. An answer that is
    not such a refusal is named by its length alone.
    """
    try:
        answered: Any = response.json()
    except ValueError:
        answered = None
    if isinstance(answered, dict):
        refusal = cast('dict[str, Any]', answered)
        fields = [f'{key} {refusal[key]!s}' for key in _REASON_FIELDS if key in refusal]
        if fields:
            return _scrubbed('; '.join(fields), text)
    return f'{len(response.text)} characters, not relayed'


def _answered(response: requests.Response, what: str, text: str) -> requests.Response:
    """A success, or the failure named with the controller's own reason.

    Anything but a 2xx fails, redirects included: a redirect is not followed,
    since it would carry the key's header to wherever it points.
    """
    if not 200 <= response.status_code < 300:
        raise WriteRefused(f'{what} answered {response.status_code}: {_reason(response, text)}')
    return response


def _held(response: requests.Response, what: str) -> dict[str, Any] | None:
    """The site's one configuration out of a GET's list, or `None` where the list is empty."""
    listed = cast('list[dict[str, Any]]', response.json())
    if len(listed) > 1:
        raise ConfigurationNotStored(f'{what} answered {len(listed)} configurations, where a site holds one')
    return listed[0] if listed else None


@final
class BgpConfigurationProvider(ConfiguredProvider):
    """The site's BGP configuration, through the controller's v2 API."""

    api_key: str

    def _read_credential(self, config: dynamic.Config) -> None:
        self.api_key = str(config.require(API_KEY_CONFIG))

    def _credential(self) -> str:
        return self.api_key

    def _endpoint(self, props: Mapping[str, Any]) -> str:
        return _base(props)

    def _version(self) -> str:
        return VERSION

    def check(self, _olds: dict[str, Any], news: dict[str, Any]) -> dynamic.CheckResult:
        return self._stamp(news, [])

    def create(self, props: dict[str, Any]) -> dynamic.CreateResult:
        session = self._opened(props)
        held = self._read(session, props)
        if held is None:
            self._write(session, props)
        elif _differences(held, props):
            raise ForeignConfiguration(
                f'site {props["site"]} holds the BGP configuration {_named(held)}, and '
                f'{"; ".join(_differences(held, props))}: delete it in the console, and the next run writes the '
                'declared one'
            )
        else:
            pulumi.log.info(f'site {props["site"]} already holds the declared BGP configuration')
        return dynamic.CreateResult(id_=str(props['site']), outs={**props, ENABLED: True})

    def read(self, id_: str, props: dict[str, Any]) -> dynamic.ReadResult:
        held = self._read(self._opened(props), props)
        if held is None:
            # Dropping the identifier is how the engine learns the configuration
            # is gone, and the next run creates it; the outputs are a fresh
            # empty bag, which the dynamic-provider host writes its own key into.
            return dynamic.ReadResult(id_=None, outs={})
        observed = {declared: held.get(stored) for declared, stored in _FIELDS.items()}
        return dynamic.ReadResult(id_=id_, outs={**props, **observed, ENABLED: held.get('enabled') is True})

    def diff(self, _id: str, olds: dict[str, Any], news: dict[str, Any]) -> dynamic.DiffResult:
        if has_unknowns(news):
            return dynamic.DiffResult(changes=None)
        replaces = [key for key in IDENTITY if olds.get(key) != news.get(key)]
        if replaces:
            # Delete-first, as the resource declares for every replacement
            # (the module docstring): another site is another object, and the
            # order costs a session that is moving anyway.
            return dynamic.DiffResult(changes=True, replaces=replaces, delete_before_replace=True)
        if declared_change(olds, news, COMPARED):
            return dynamic.DiffResult(changes=True, replaces=[])
        # What a refresh recorded of the switch is not asked here: the read
        # below sees the switch as the controller holds it now.
        held = self._read(self._opened(news), news)
        return dynamic.DiffResult(changes=held is None or bool(_differences(held, news)), replaces=[])

    def update(self, _id: str, olds: dict[str, Any], news: dict[str, Any]) -> dynamic.UpdateResult:
        moved_alone = declared_change(olds, news, (*TRANSPORT, *STAMPS)) and not (
            declared_change(olds, news, CONTENT) or olds.get(ENABLED) is False
        )
        if not moved_alone:
            self._write(self._opened(news), news)
        # The outs replace the stored output bag (framework/pulumi.md §5.3 E9),
        # so what state says about the door this configuration was written
        # through stays true.
        return dynamic.UpdateResult(outs={**news, ENABLED: True})

    def delete(self, _id: str, props: dict[str, Any]) -> None:
        session = self._opened(props)
        if self._read(session, props) is None:
            return
        url = _url(props)
        pulumi.log.info(f'deleting the BGP configuration of site {props["site"]}')
        _ = _answered(session.delete(url, timeout=TIMEOUT, allow_redirects=False), f'DELETE {url}', '')
        left = self._read(session, props)
        if left is not None:
            raise ConfigurationNotStored(f'site {props["site"]} still holds {_named(left)} after its DELETE')

    def _opened(self, props: Mapping[str, Any]) -> requests.Session:
        session = requests.Session()
        session.headers[API_KEY_HEADER] = self.api_key
        session.verify = not props['allow_insecure']
        if props['allow_insecure']:
            # The posture is the caller's, stated once where it is declared; a
            # warning per request would stand above every plan.
            urllib3.disable_warnings(InsecureRequestWarning)
        return session

    def _read(self, session: requests.Session, props: Mapping[str, Any]) -> dict[str, Any] | None:
        url = _url(props)
        pulumi.log.info(f'reading the BGP configuration of site {props["site"]}')
        response = _answered(session.get(url, timeout=TIMEOUT, allow_redirects=False), f'GET {url}', '')
        return _held(response, f'GET {url}')

    def _write(self, session: requests.Session, props: Mapping[str, Any]) -> None:
        """Send the whole object, and read back that the controller holds it."""
        url = _url(props)
        text = str(props['config'])
        pulumi.log.info(f'writing the BGP configuration of site {props["site"]}')
        sent = session.post(url, json=_object(props), timeout=TIMEOUT, allow_redirects=False)
        _ = _answered(sent, f'POST {url}', text)
        held = self._read(session, props)
        if held is None:
            raise ConfigurationNotStored(f'site {props["site"]} holds no BGP configuration after its POST')
        differences = _differences(held, props)
        if differences:
            raise ConfigurationNotStored(
                f'site {props["site"]} holds {_named(held)} after its POST, and {"; ".join(differences)}'
            )


@final
class BgpConfiguration(dynamic.Resource, module='unifi_bgp', name='BgpConfiguration'):
    """The site's BGP configuration: `config`, switched on, and nothing else."""

    site: pulumi.Output[str]
    #: The session's password is in it: a secret in state and in every preview.
    config: Annotated[pulumi.Output[str], Secret]
    description: pulumi.Output[str]
    file_name: pulumi.Output[str]

    def __init__(
        self,
        name: str,
        *,
        api_url: pulumi.Input[str],
        allow_insecure: pulumi.Input[bool],
        site: pulumi.Input[str],
        config: pulumi.Input[str],
        description: pulumi.Input[str],
        file_name: pulumi.Input[str],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        """Declare that the site's BGP configuration is `config`, switched on.

        `config` is made a secret here whatever it arrives as, input and output
        alike, because what it carries is the routing session's password. The
        description and the file name are what the console shows beside it, and
        are public; the key that writes it is not a property at all.

        Every replacement is delete-first, because `create` adopts an identical
        configuration and `delete` removes whatever the site holds (the module
        docstring).
        """
        super().__init__(
            BgpConfigurationProvider(),
            name,
            {
                'api_url': api_url,
                'allow_insecure': allow_insecure,
                'site': site,
                'config': pulumi.Output.secret(config),
                'description': description,
                'file_name': file_name,
            },
            pulumi.ResourceOptions.merge(
                pulumi.ResourceOptions(additional_secret_outputs=['config'], delete_before_replace=True), opts
            ),
        )
