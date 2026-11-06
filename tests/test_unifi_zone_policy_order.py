"""The order on a zone pair, against the stand-in for the controller's v2 API.

The order's reason to exist is one refusal: the controller takes a pair's rank
only as a batch naming every custom policy of the pair, and a list declared in
a program is not that batch while the census's own edits are in transit. So the
first case runs what `SiteFirewall` declares against a controller that refuses
the bridged batch, and the rest are what the resource does with what it reads:
the census first; a foreign policy refused by name and reported on refresh; a
policy in transit carried; the controller's own derivative left alone.

**A provider under test is configured first**, as one in production is
(framework/pulumi.md §5.3 E2), and each operation is handed the bag the engine
hands it: `create` the checked inputs, `diff`, `update` and `read` the stored
outputs, which `Stored` keeps the way state does (§5.3 E7, E9).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pulumi
import pulumi.dynamic as dynamic
import pytest
import requests
from mock_monitor import declaring, run_with
from pulumi.runtime import rpc
from unifi_controller import FIRST_CUSTOM, FIRST_PREDEFINED, Controller, ZonePolicies, refusal

from kluster.components.gateway import unifi
from kluster.providers import unifi_zone_policy_order as order

PROJECT = 'kluster'
KEY = 'unifi-api-key'
SITE = 'default'
API_URL = 'https://gateway.invalid'
SOURCE = 'zone-source'
DESTINATION = 'zone-destination'


def configured() -> order.ZonePolicyOrderProvider:
    """A provider as an operation receives one: revived, then handed the config."""
    provider = order.ZonePolicyOrderProvider()
    provider.configure(
        dynamic.ConfigureRequest(config=dynamic.Config({f'{PROJECT}:{order.API_KEY_CONFIG}': KEY}, PROJECT))
    )
    return provider


def declared(policy_ids: list[str], **changed: Any) -> dict[str, Any]:
    """The inputs of an order on the suite's pair."""
    return {
        'api_url': API_URL,
        'allow_insecure': True,
        'site': SITE,
        'source_zone_id': SOURCE,
        'destination_zone_id': DESTINATION,
        'policy_ids': policy_ids,
        **changed,
    }


def checked(inputs: dict[str, Any]) -> dict[str, Any]:
    return configured().check({}, inputs).inputs


@dataclass
class Stored:
    """One order's life, driven through the provider the way the engine drives it."""

    outs: dict[str, Any] = field(default_factory=dict[str, Any])
    id_: str = ''

    def create(self, inputs: dict[str, Any]) -> None:
        created = configured().create(checked(inputs))
        assert created.outs is not None
        self.id_, self.outs = created.id, created.outs

    def refresh(self) -> list[str]:
        """Replace the stored outputs with `read`'s, and answer what it reported."""
        read = configured().read(self.id_, self.outs)
        assert read.outs is not None
        self.outs = read.outs
        return list(self.outs[order.DRIFT])

    def plans_an_update(self, inputs: dict[str, Any]) -> bool:
        return bool(configured().diff(self.id_, self.outs, checked(inputs)).changes)

    def update(self, inputs: dict[str, Any]) -> None:
        updated = configured().update(self.id_, self.outs, checked(inputs))
        assert updated.outs is not None
        self.outs = updated.outs


@pytest.fixture
def controller(monkeypatch: pytest.MonkeyPatch) -> ZonePolicies:
    """The site, holding the suite's pair and nothing custom on it."""
    served = ZonePolicies(key=KEY, site=SITE)
    served.pair(SOURCE, DESTINATION)
    monkeypatch.setattr(requests, 'Session', served.session)
    return served


def bridged_batch(policy_ids: list[str]) -> dict[str, Any]:
    """What the bridged order's `applyReorder` sends: the declared lists and nothing else."""
    return {
        'source_zone_id': SOURCE,
        'destination_zone_id': DESTINATION,
        'before_predefined_ids': policy_ids,
        'after_predefined_ids': [],
    }


@pytest.mark.asyncio
async def test_every_order_the_census_declares_survives_the_edit_the_bridged_batch_is_refused_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A replacement is on the pair beside its original when the pair's order runs.

    The program's orders, each on a stand-in holding the census's policies:
    the bridged batch for the new list leaves the original out and is refused,
    and the order the program declares ranks the replacement first all the
    same. This is the change: declared as the bridged resource, the program
    leaves this case nothing to run.
    """
    mocks = await run_with(Controller(site=SITE), stack='physical')
    pulumi.runtime.set_all_config({f'{PROJECT}:{unifi.API_KEY}': KEY})
    async with declaring():
        unifi.SiteFirewall(PROJECT, api_url=API_URL, site=SITE, worker_gua='2001:db8:1:70::10', static_hosts={})
    orders = mocks.by_name('pulumi-python:dynamic/unifi_zone_policy_order:ZonePolicyOrder')
    assert orders, 'the program declared no order of this kind'

    for name, inputs in orders.items():
        served = ZonePolicies(key=KEY, site=str(inputs['site']))
        monkeypatch.setattr(requests, 'Session', served.session)
        source, destination = str(inputs['source_zone_id']), str(inputs['destination_zone_id'])
        served.pair(source, destination)
        held = {policy: served.add(policy, source, destination) for policy in inputs['policy_ids']}
        life = Stored()
        life.create({**inputs, 'policy_ids': list(held.values())})

        first, *rest = held.values()
        replacement = served.add('replacement', source, destination)
        new_list = [replacement, *rest]

        batch = bridged_batch(new_list) | {'source_zone_id': source, 'destination_zone_id': destination}
        session = served.session()
        session.headers['X-API-KEY'] = KEY
        refused = session.put(f'{API_URL}/proxy/network/v2/api/site/{SITE}/firewall-policies/batch-reorder', batch)
        assert refused.status_code == 400, name

        life.update({**inputs, 'policy_ids': new_list})
        assert [policy['_id'] for policy in served.custom(source, destination)][: len(new_list)] == new_list, name
        served.delete(first)
        assert life.refresh() == [], name


def test_the_census_is_ranked_first_in_the_rows_order(controller: ZonePolicies) -> None:
    """The row's order, against the order creation happened to leave."""
    allow = controller.add('allow', SOURCE, DESTINATION)
    drop = controller.add('drop', SOURCE, DESTINATION)

    Stored().create(declared([drop, allow]))

    assert controller.ranked(SOURCE, DESTINATION) == ['drop', 'allow']
    (batch,) = controller.batches
    assert batch['before_predefined_ids'] == [drop, allow]
    assert batch['after_predefined_ids'] == []


def test_a_foreign_policy_is_refused_by_name_and_left_where_it_is(controller: ZonePolicies) -> None:
    """A census pair belongs to the census whole, and the census takes nothing from the console.

    The order neither adopts it, which would send it in the batch, nor moves it,
    nor deletes it: nothing is sent at all.
    """
    census = controller.add('census', SOURCE, DESTINATION)
    controller.add('Hand rule', SOURCE, DESTINATION)

    with pytest.raises(order.ForeignPolicy, match='Hand rule'):
        Stored().create(declared([census]))

    assert controller.batches == []
    assert controller.ranked(SOURCE, DESTINATION) == ['census', 'Hand rule']


def test_a_foreign_policy_reads_as_drift_and_the_update_it_proposes_refuses(controller: ZonePolicies) -> None:
    """The refreshed preview says so, and no refresh turns the policy into the census's.

    The refresh writes the stored bag the update then reads as its old inputs,
    so a list rewritten from the controller would hand the foreign policy the
    pass a policy in transit gets.
    """
    census = controller.add('census', SOURCE, DESTINATION)
    life = Stored()
    life.create(declared([census]))
    assert life.refresh() == []
    assert not life.plans_an_update(declared([census]))
    written = len(controller.batches)

    foreign = controller.add('Hand rule', SOURCE, DESTINATION)
    drift = life.refresh()

    assert any('Hand rule' in finding and foreign in finding for finding in drift), drift
    assert life.plans_an_update(declared([census]))
    with pytest.raises(order.ForeignPolicy, match='Hand rule'):
        life.update(declared([census]))
    assert len(controller.batches) == written
    controller.delete(foreign)
    assert life.refresh() == []
    assert not life.plans_an_update(declared([census]))


def test_a_listed_policy_the_pair_does_not_hold_fails_by_name(controller: ZonePolicies) -> None:
    """Sent anyway, the batch is refused for an id, with nothing to say whose."""
    census = controller.add('census', SOURCE, DESTINATION)
    elsewhere = controller.add('elsewhere', DESTINATION, SOURCE)

    with pytest.raises(order.ListedPolicyAbsent, match=elsewhere):
        Stored().create(declared([census, elsewhere]))

    assert controller.batches == []


def test_a_replacement_ranks_first_with_its_original_carried_behind_it(controller: ZonePolicies) -> None:
    """Create before delete: the original is on the pair and unlisted when the order updates."""
    original = controller.add('original', SOURCE, DESTINATION)
    other = controller.add('other', SOURCE, DESTINATION)
    life = Stored()
    life.create(declared([original, other]))

    replacement = controller.add('replacement', SOURCE, DESTINATION)
    life.update(declared([replacement, other]))

    assert controller.ranked(SOURCE, DESTINATION) == ['replacement', 'other', 'original']
    assert life.outs[order.DRIFT] == []
    controller.delete(original)
    assert life.refresh() == []
    assert not life.plans_an_update(declared([replacement, other]))


def test_a_dropped_policy_is_carried_until_its_own_delete(controller: ZonePolicies) -> None:
    """A policy the program stopped declaring goes after the updates that stopped naming it."""
    kept = controller.add('kept', SOURCE, DESTINATION)
    dropped = controller.add('dropped', SOURCE, DESTINATION)
    life = Stored()
    life.create(declared([dropped, kept]))

    life.update(declared([kept]))

    assert controller.ranked(SOURCE, DESTINATION) == ['kept', 'dropped']
    controller.delete(dropped)
    assert life.refresh() == []


def test_the_old_list_excuses_only_what_it_named(controller: ZonePolicies) -> None:
    """A policy in transit is no reason to send a foreign one beside it."""
    original = controller.add('original', SOURCE, DESTINATION)
    life = Stored()
    life.create(declared([original]))

    replacement = controller.add('replacement', SOURCE, DESTINATION)
    controller.add('Hand rule', SOURCE, DESTINATION)

    with pytest.raises(order.ForeignPolicy, match='Hand rule'):
        life.update(declared([replacement]))


def test_the_controllers_return_policy_is_no_drift(controller: ZonePolicies) -> None:
    """A flagged allow's derivative lands on a pair the census may order.

    It is the controller's, and predefined, so it is left out of the batch and
    the census ranks ahead of it.
    """
    census = controller.add('census', SOURCE, DESTINATION)
    derivative = controller.derive('kluster allow', SOURCE, DESTINATION)
    life = Stored()

    life.create(declared([census]))

    assert derivative not in controller.batches[0]['before_predefined_ids']
    assert life.refresh() == []
    assert not life.plans_an_update(declared([census]))


@pytest.mark.parametrize(
    'moved',
    [
        pytest.param(FIRST_CUSTOM - 1, id='the second census policy ahead of the first'),
        pytest.param(FIRST_PREDEFINED + 1, id='a census policy behind the predefined ones'),
    ],
)
def test_a_census_policy_out_of_place_is_reported_and_ranked_first_again(controller: ZonePolicies, moved: int) -> None:
    first = controller.add('first', SOURCE, DESTINATION)
    second = controller.add('second', SOURCE, DESTINATION)
    life = Stored()
    life.create(declared([first, second]))

    controller.move(second, moved)

    assert life.refresh() != []
    assert life.plans_an_update(declared([first, second]))
    life.update(declared([first, second]))
    assert controller.ranked(SOURCE, DESTINATION) == ['first', 'second']
    assert life.refresh() == []


def test_a_foreign_policy_ranked_ahead_of_the_census_is_reported_as_both(controller: ZonePolicies) -> None:
    """The reading the scheduled check leans on: the rank, and whose policy took it."""
    census = controller.add('census', SOURCE, DESTINATION)
    life = Stored()
    life.create(declared([census]))

    foreign = controller.add('Hand rule', SOURCE, DESTINATION)
    controller.move(foreign, FIRST_CUSTOM - 1)

    drift = life.refresh()
    assert len([finding for finding in drift if foreign in finding]) == 2, drift


def test_an_address_or_a_stamp_that_moved_sends_nothing(controller: ZonePolicies) -> None:
    """The rank is the same rank, so an update reached for either records it and writes nothing."""
    census = controller.add('census', SOURCE, DESTINATION)
    life = Stored()
    life.create(declared([census]))
    written = len(controller.batches)

    moved = declared([census], api_url='https://10.0.5.1')
    assert life.plans_an_update(moved)
    life.update(moved)

    assert len(controller.batches) == written
    assert life.outs[order.DRIFT] == []
    assert life.outs['api_url'] == 'https://10.0.5.1'


def test_another_pair_is_another_rank(controller: ZonePolicies) -> None:
    census = controller.add('census', SOURCE, DESTINATION)
    life = Stored()
    life.create(declared([census]))

    planned = configured().diff(life.id_, life.outs, checked(declared([census], source_zone_id='zone-other')))

    assert planned.replaces == ['source_zone_id']


@pytest.mark.parametrize(
    'inputs',
    [
        pytest.param(declared(['policy-1', rpc.UNKNOWN]), id='a replacement in the list'),
        pytest.param(declared([]) | {'policy_ids': rpc.UNKNOWN}, id='the whole list'),
        pytest.param(declared(['policy-1'], source_zone_id=rpc.UNKNOWN), id='a zone'),
    ],
)
def test_a_value_nobody_knows_yet_plans_nothing_of_its_own(controller: ZonePolicies, inputs: dict[str, Any]) -> None:
    """Not a replacement, and not a claim that nothing changed: the engine decides on the unknown."""
    census = controller.add('census', SOURCE, DESTINATION)
    life = Stored()
    life.create(declared([census]))

    planned = configured().diff(life.id_, life.outs, checked(inputs))

    assert planned.changes is None
    assert not planned.replaces


def test_a_console_edit_between_the_read_and_the_write_is_read_again(controller: ZonePolicies) -> None:
    """The one benign refusal: the policy carried is deleted after the read took it."""
    original = controller.add('original', SOURCE, DESTINATION)
    life = Stored()
    life.create(declared([original]))
    replacement = controller.add('replacement', SOURCE, DESTINATION)
    controller.before_next_write = lambda: controller.delete(original)

    life.update(declared([replacement]))

    assert controller.ranked(SOURCE, DESTINATION) == ['replacement']
    assert len(controller.batches) == 3


def test_a_policy_the_console_adds_after_the_read_is_refused_by_name(controller: ZonePolicies) -> None:
    """The batch that left it out is refused, and the second read names it."""
    census = controller.add('census', SOURCE, DESTINATION)
    controller.before_next_write = lambda: (controller.add('Hand rule', SOURCE, DESTINATION), None)[1]

    with pytest.raises(order.ForeignPolicy, match='Hand rule'):
        Stored().create(declared([census]))


def test_a_refusal_that_outlives_the_retry_carries_the_controllers_reason(
    controller: ZonePolicies, monkeypatch: pytest.MonkeyPatch
) -> None:
    census = controller.add('census', SOURCE, DESTINATION)
    answered: list[str] = []

    def refuses(_batch: dict[str, Any]) -> Any:
        answered.append('refused')
        return refusal('Something the controller has to say')

    monkeypatch.setattr(controller, '_reorder', refuses)

    with pytest.raises(order.BatchRefused, match='Something the controller has to say'):
        Stored().create(declared([census]))
    assert len(answered) == 2


def test_a_batch_that_took_is_read_back(controller: ZonePolicies) -> None:
    """An answer of 200 is not the rank: a write that changes nothing fails here rather than reading as done."""
    first = controller.add('first', SOURCE, DESTINATION)
    second = controller.add('second', SOURCE, DESTINATION)
    controller.ignores_rank = True

    with pytest.raises(order.ListOutOfPlace):
        Stored().create(declared([second, first]))


def test_a_pair_with_no_predefined_policy_has_nothing_to_rank_ahead_of(controller: ZonePolicies) -> None:
    census = controller.add('census', 'zone-bare', DESTINATION)

    with pytest.raises(order.NoPredefinedPolicy):
        Stored().create(declared([census], source_zone_id='zone-bare'))


def test_the_key_is_the_sessions_and_no_propertys(controller: ZonePolicies) -> None:
    """The key travels as a header, the posture is the input's, and neither lands on the resource as a secret."""
    census = controller.add('census', SOURCE, DESTINATION)
    life = Stored()

    life.create(declared([census]))

    assert KEY not in str(life.outs)
    assert all(session.headers['X-API-KEY'] == KEY for session in controller.sessions)
    assert all(session.verify is False for session in controller.sessions)
    life.update(declared([census], allow_insecure=False, api_url='https://other.invalid'))
    assert life.refresh() == []
    assert controller.sessions[-1].verify is True


def test_a_key_the_controller_refuses_fails_rather_than_reading_as_an_empty_pair(controller: ZonePolicies) -> None:
    """An empty pair would read as every listed policy missing; the refusal says what is wrong."""
    census = controller.add('census', SOURCE, DESTINATION)
    controller.key = 'another-key'

    with pytest.raises(RuntimeError, match='401'):
        Stored().create(declared([census]))


def test_a_redirect_fails_and_is_not_followed(controller: ZonePolicies) -> None:
    """Followed, it would hand the key's header to wherever it points."""
    census = controller.add('census', SOURCE, DESTINATION)
    controller.redirects = True

    with pytest.raises(RuntimeError, match='302'):
        Stored().create(declared([census]))


def test_delete_sends_nothing(controller: ZonePolicies) -> None:
    """A rank is no object, and the policies go through their own resources."""
    census = controller.add('census', SOURCE, DESTINATION)
    life = Stored()
    life.create(declared([census]))
    opened = len(controller.sessions)

    configured().delete(life.id_, life.outs)

    assert len(controller.sessions) == opened
    assert controller.ranked(SOURCE, DESTINATION) == ['census']
