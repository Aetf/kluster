"""The site's BGP configuration, against the stand-in for the controller's v2 API.

The resource holds the site's one configuration whole: it creates it on a site
holding none, takes an identical one as created, refuses any other by name,
reads every write back, sees a console edit in `diff` without a refresh, and
deletes it. The text carries the routing session's password, so the last cases
hold every message the resource can raise or log to carrying none of it.

**A provider under test is configured first**, as one in production is
(framework/pulumi.md §5.3 E2), and each operation is handed the bag the engine
hands it: `create` the checked inputs, `diff`, `update` and `read` the stored
outputs, which `Stored` keeps the way state does (§5.3 E7, E9).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pulumi.dynamic as dynamic
import pytest
import requests
from mock_monitor import Recorder, declaring, run_with
from pulumi.runtime import rpc
from unifi_bgp_controller import BgpConfigs, refusal

from kluster.components.gateway import unifi
from kluster.providers import unifi_bgp as bgp

PROJECT = 'kluster'
KEY = 'unifi-api-key'
SITE = 'default'
API_URL = 'https://gateway.invalid'
PASSWORD = 'a-session-password-7f3e'
CONFIG = f'router bgp 65000\n neighbor 192.0.2.10 remote-as 65001\n neighbor 192.0.2.10 password {PASSWORD}\n!\n'
DESCRIPTION = 'Managed by a program.'
FILE_NAME = 'program-frr.conf'

#: How a secret arrives on the wire: Pulumi's special-signature key, carrying
#: the signature that means "secret", beside the value itself.
SECRET = {'4dabf18193072939515e22adb298388d': '1b47061264138c4ac30d75fd1eb44270'}


def configured(key: str = KEY) -> bgp.BgpConfigurationProvider:
    """A provider as an operation receives one: revived, then handed the config."""
    provider = bgp.BgpConfigurationProvider()
    provider.configure(
        dynamic.ConfigureRequest(config=dynamic.Config({f'{PROJECT}:{bgp.API_KEY_CONFIG}': key}, PROJECT))
    )
    return provider


def declared(**changed: Any) -> dict[str, Any]:
    """The inputs of the suite's configuration."""
    return {
        'api_url': API_URL,
        'allow_insecure': True,
        'site': SITE,
        'config': CONFIG,
        'description': DESCRIPTION,
        'file_name': FILE_NAME,
        **changed,
    }


def checked(inputs: dict[str, Any], key: str = KEY) -> dict[str, Any]:
    return configured(key).check({}, inputs).inputs


@dataclass
class Stored:
    """One configuration's life, driven through the provider the way the engine drives it."""

    outs: dict[str, Any] = field(default_factory=dict[str, Any])
    id_: str | None = ''

    def create(self, inputs: dict[str, Any]) -> None:
        created = configured().create(checked(inputs))
        assert created.outs is not None
        self.id_, self.outs = created.id, created.outs

    def refresh(self) -> None:
        """Replace the stored outputs with `read`'s."""
        read = configured().read(str(self.id_), self.outs)
        assert read.outs is not None
        self.id_, self.outs = read.id, read.outs

    def plans_an_update(self, inputs: dict[str, Any], key: str = KEY) -> bool | None:
        return configured(key).diff(str(self.id_), self.outs, checked(inputs, key)).changes

    def update(self, inputs: dict[str, Any], key: str = KEY) -> None:
        updated = configured(key).update(str(self.id_), self.outs, checked(inputs, key))
        assert updated.outs is not None
        self.outs = updated.outs

    def delete(self) -> None:
        configured().delete(str(self.id_), self.outs)


@pytest.fixture
def controller(monkeypatch: pytest.MonkeyPatch) -> BgpConfigs:
    """The site, holding no BGP configuration, as it was read before any was written."""
    served = BgpConfigs(key=KEY, site=SITE)
    monkeypatch.setattr(requests, 'Session', served.session)
    return served


def created(controller: BgpConfigs) -> Stored:
    """The suite's configuration, created on the empty site, with the controller's request log cleared."""
    life = Stored()
    life.create(declared())
    controller.requests.clear()
    controller.writes.clear()
    return life


def writes(controller: BgpConfigs) -> int:
    return sum(method != 'GET' for method, _ in controller.requests)


##
## Create
##


def test_a_site_holding_none_is_sent_the_whole_object_switched_on(controller: BgpConfigs) -> None:
    created(controller)

    assert controller.held is not None
    assert controller.held['frr_bgpd_config'] == CONFIG
    assert controller.held['enabled'] is True
    assert (controller.held['description'], controller.held['uploaded_file_name']) == (DESCRIPTION, FILE_NAME)


def test_a_site_holding_another_configuration_is_refused_by_name_and_left_as_it_is(controller: BgpConfigs) -> None:
    """The resource neither overwrites a configuration it did not write nor adopts it."""
    console = controller.hold(config='router bgp 64512\n', description='Hand upload', file_name='mine.conf')
    before = dict(console)

    with pytest.raises(bgp.ForeignConfiguration, match='Hand upload'):
        Stored().create(declared())

    assert writes(controller) == 0
    assert controller.held == before


def test_a_site_holding_the_declared_configuration_switched_off_is_refused(controller: BgpConfigs) -> None:
    """Switched off is not what is declared, so it is not taken as this resource's."""
    _ = controller.hold(config=CONFIG, description=DESCRIPTION, file_name=FILE_NAME, enabled=False)

    with pytest.raises(bgp.ForeignConfiguration):
        Stored().create(declared())

    assert writes(controller) == 0


def test_a_site_already_holding_the_declared_configuration_is_created_with_no_write(controller: BgpConfigs) -> None:
    """A run that failed after its write runs again."""
    _ = controller.hold(config=CONFIG, description=DESCRIPTION, file_name=FILE_NAME)

    life = Stored()
    life.create(declared())

    assert writes(controller) == 0
    assert life.id_ == SITE
    assert not life.plans_an_update(declared())


def _without_trailing_newline(text: str) -> str:
    return text.rstrip('\n')


def _with_a_line_rewritten(text: str) -> str:
    return text.replace(' password ', ' password X')


@pytest.mark.parametrize('stores', [_without_trailing_newline, _with_a_line_rewritten])
def test_a_write_the_controller_stores_differently_fails(controller: BgpConfigs, stores: Callable[[str], str]) -> None:
    controller.stores = stores

    with pytest.raises(bgp.ConfigurationNotStored, match='the configuration text differs'):
        Stored().create(declared())


def test_a_site_answered_as_holding_two_configurations_is_refused(controller: BgpConfigs) -> None:
    """A site holds one; a list of two is not read as either of them."""
    _ = controller.hold(config=CONFIG, description=DESCRIPTION, file_name=FILE_NAME)
    controller.also_listed.append({'_id': 'bgp-other', 'enabled': True, 'frr_bgpd_config': ''})

    with pytest.raises(bgp.ConfigurationNotStored, match='2 configurations'):
        Stored().create(declared())

    assert writes(controller) == 0


def test_a_refused_write_fails_with_the_controllers_reason(controller: BgpConfigs) -> None:
    controller.refuses_next_write = refusal('Invalid BGP configuration')

    with pytest.raises(bgp.WriteRefused, match=r'400.*Invalid BGP configuration'):
        Stored().create(declared())


##
## Diff and update
##


def test_a_clean_site_plans_nothing(controller: BgpConfigs) -> None:
    life = created(controller)

    assert life.plans_an_update(declared()) is False


@pytest.mark.parametrize(
    ('edit', 'field'),
    [
        pytest.param({'frr_bgpd_config': CONFIG.replace('65001', '65002')}, 'frr_bgpd_config', id='text'),
        pytest.param({'enabled': False}, 'enabled', id='switched-off'),
        pytest.param({'description': 'edited'}, 'description', id='description'),
    ],
)
def test_a_console_edit_is_planned_without_a_refresh_and_written_back(
    controller: BgpConfigs, edit: dict[str, Any], field: str
) -> None:
    life = created(controller)
    assert controller.held is not None
    expected = controller.held[field]
    controller.held.update(edit)

    assert life.plans_an_update(declared()) is True
    life.update(declared())

    assert controller.held[field] == expected
    assert life.plans_an_update(declared()) is False


def test_a_configuration_deleted_in_the_console_is_planned_without_a_refresh(controller: BgpConfigs) -> None:
    life = created(controller)
    controller.held = None

    assert life.plans_an_update(declared()) is True
    life.update(declared())

    assert controller.held is not None
    assert controller.held['frr_bgpd_config'] == CONFIG


def test_a_declared_change_is_written_and_keeps_the_object(controller: BgpConfigs) -> None:
    life = created(controller)
    assert controller.held is not None
    kept = controller.held['_id']
    edited = CONFIG.replace('65001', '65009')

    assert life.plans_an_update(declared(config=edited)) is True
    life.update(declared(config=edited))

    assert controller.held['frr_bgpd_config'] == edited
    assert controller.held['_id'] == kept
    assert [write['frr_bgpd_config'] for write in controller.writes] == [edited]


def test_an_update_the_controller_stores_differently_fails(controller: BgpConfigs) -> None:
    life = created(controller)
    controller.stores = lambda text: text.upper()

    with pytest.raises(bgp.ConfigurationNotStored):
        life.update(declared(config=CONFIG.replace('65001', '65009')))


@pytest.mark.parametrize(
    ('inputs', 'key'),
    [
        pytest.param(declared(api_url='https://gateway.elsewhere.invalid'), KEY, id='address'),
        pytest.param(declared(), 'rotated-key', id='key'),
    ],
)
def test_an_update_reached_for_a_stamp_alone_calls_no_endpoint(
    controller: BgpConfigs, inputs: dict[str, Any], key: str
) -> None:
    """A moved address or a rotated key changes nothing about the configuration.

    The rotated key is not the controller's, so a request made with it would
    be refused: the update succeeds because it makes none.
    """
    life = created(controller)

    assert life.plans_an_update(inputs, key) is True
    life.update(inputs, key)

    assert controller.requests == []
    assert life.outs[bgp.ENABLED] is True


def test_another_site_is_a_replacement(controller: BgpConfigs) -> None:
    life = created(controller)

    planned = configured().diff(str(life.id_), life.outs, checked(declared(site='other')))

    assert planned.changes is True
    assert list(planned.replaces or []) == ['site']


@pytest.mark.parametrize(
    'inputs',
    [
        pytest.param(declared(config=rpc.UNKNOWN), id='config'),
        pytest.param(declared(api_url=rpc.UNKNOWN), id='address'),
    ],
)
def test_a_value_nobody_knows_yet_plans_nothing_of_its_own(controller: BgpConfigs, inputs: dict[str, Any]) -> None:
    life = created(controller)

    assert life.plans_an_update(inputs) is None
    assert controller.requests == []


##
## Refresh
##


def test_a_refresh_records_what_the_console_did_and_the_next_run_writes_it_back(controller: BgpConfigs) -> None:
    life = created(controller)
    assert controller.held is not None
    controller.held['frr_bgpd_config'] = 'router bgp 64512\n'
    controller.held['enabled'] = False

    life.refresh()

    assert life.outs['config'] == 'router bgp 64512\n'
    assert life.outs[bgp.ENABLED] is False
    life.update(declared())
    assert (controller.held['frr_bgpd_config'], controller.held['enabled']) == (CONFIG, True)


def test_a_stamp_update_after_a_refresh_found_the_page_switched_off_writes(controller: BgpConfigs) -> None:
    """The refresh's record of the switch is what plans the write, whatever else moved."""
    life = created(controller)
    assert controller.held is not None
    controller.held['enabled'] = False
    life.refresh()

    life.update(declared(api_url='https://gateway.elsewhere.invalid'))

    assert controller.held['enabled'] is True
    assert life.outs[bgp.ENABLED] is True


def test_a_refresh_of_a_configuration_gone_from_the_site_drops_it(controller: BgpConfigs) -> None:
    life = created(controller)
    controller.held = None

    life.refresh()

    assert life.id_ is None


##
## Delete
##


def test_delete_takes_the_configuration_away(controller: BgpConfigs) -> None:
    life = created(controller)

    life.delete()

    assert controller.held is None
    assert ('DELETE', f'/proxy/network/v2/api/site/{SITE}/bgp/config') in controller.requests


def test_a_delete_the_site_outlives_fails(controller: BgpConfigs) -> None:
    life = created(controller)
    controller.ignores_delete = True

    with pytest.raises(bgp.ConfigurationNotStored, match='after its DELETE'):
        life.delete()


def test_a_configuration_already_gone_is_deleted_with_no_request_to_delete(controller: BgpConfigs) -> None:
    life = created(controller)
    controller.held = None

    life.delete()

    assert writes(controller) == 0


##
## The session
##


@pytest.mark.parametrize('allow_insecure', [True, False])
def test_the_key_is_the_sessions_and_the_posture_the_inputs(controller: BgpConfigs, allow_insecure: bool) -> None:
    life = Stored()
    life.create(declared(allow_insecure=allow_insecure))

    assert controller.sessions
    assert all(session.headers['X-API-KEY'] == KEY for session in controller.sessions)
    assert all(session.verify is not allow_insecure for session in controller.sessions)
    assert KEY not in repr(life.outs)


def test_a_key_the_controller_refuses_fails_rather_than_reading_as_an_empty_site(controller: BgpConfigs) -> None:
    controller.key = 'another'

    with pytest.raises(bgp.WriteRefused, match='401'):
        Stored().create(declared())

    assert controller.held is None


def test_a_redirect_fails_and_is_not_followed(controller: BgpConfigs) -> None:
    controller.redirects = True

    with pytest.raises(bgp.WriteRefused, match='302'):
        Stored().create(declared())


##
## The text is a secret
##


def _said(raised: pytest.ExceptionInfo[BaseException], capsys: pytest.CaptureFixture[str]) -> str:
    captured = capsys.readouterr()
    return f'{raised.value}\n{captured.out}\n{captured.err}'


def test_a_refusal_quoting_the_text_relays_its_reason_without_the_text(
    controller: BgpConfigs, capsys: pytest.CaptureFixture[str]
) -> None:
    """A parser's complaint quotes the line it refused, and the line holds the password."""
    line = CONFIG.splitlines()[2].strip()
    controller.refuses_next_write = refusal(f'Unknown command: "{line}"')

    with pytest.raises(bgp.WriteRefused) as raised:
        Stored().create(declared())

    said = _said(raised, capsys)
    assert 'Unknown command' in said
    assert PASSWORD not in said
    assert line not in said


def test_a_short_word_of_the_text_is_taken_out_where_it_stands_alone(
    controller: BgpConfigs, capsys: pytest.CaptureFixture[str]
) -> None:
    """A word too short to be taken out of the middle of another is taken out where it is a word of its own."""
    short = 'xyz'
    controller.refuses_next_write = refusal(f'{short} refused; see xyzzy')

    with pytest.raises(bgp.WriteRefused) as raised:
        Stored().create(declared(config=f'router bgp 65000\n neighbor 192.0.2.10 password {short}\n'))

    said = _said(raised, capsys)
    assert f'{short} refused' not in said
    assert 'xyzzy' in said


def test_a_refusal_that_is_no_v2_refusal_is_named_by_its_length(
    controller: BgpConfigs, capsys: pytest.CaptureFixture[str]
) -> None:
    controller.refuses_next_write = refusal('')
    controller.refuses_next_write.text = f'<html>{CONFIG}</html>'

    with pytest.raises(bgp.WriteRefused) as raised:
        Stored().create(declared())

    said = _said(raised, capsys)
    assert 'characters, not relayed' in said
    assert PASSWORD not in said


def test_a_foreign_configuration_is_named_without_its_text_or_the_declared_one(
    controller: BgpConfigs, capsys: pytest.CaptureFixture[str]
) -> None:
    _ = controller.hold(config=CONFIG.replace('65001', '65002'), description='Hand upload', file_name='mine.conf')

    with pytest.raises(bgp.ForeignConfiguration) as raised:
        Stored().create(declared())

    said = _said(raised, capsys)
    assert PASSWORD not in said
    assert 'remote-as' not in said


def test_a_write_stored_differently_is_named_without_either_text(
    controller: BgpConfigs, capsys: pytest.CaptureFixture[str]
) -> None:
    controller.stores = lambda text: text.replace('65001', '65002')

    with pytest.raises(bgp.ConfigurationNotStored) as raised:
        Stored().create(declared())

    said = _said(raised, capsys)
    assert PASSWORD not in said
    assert 'remote-as' not in said


##
## The program's declaration
##


@pytest.mark.asyncio
async def test_the_configuration_is_secret_whatever_it_arrives_as() -> None:
    """The text is the session's password's carrier, so the resource marks it secret itself."""
    recorder = await run_with(Recorder(), stack='physical')
    async with declaring():
        _ = unifi.site_bgp(
            'bgp', api_url=API_URL, site=SITE, config=CONFIG, description=DESCRIPTION, file_name=FILE_NAME
        )

    assert recorder.inputs_of('bgp')['config'] == SECRET | {'value': CONFIG}
    assert 'config' in recorder.options_of('bgp').additionalSecretOutputs
