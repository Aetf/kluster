"""The spending guardrails.

A quota policy is a text document the service parses, so the things worth
asserting are textual: that the default-deny statement precedes the exceptions
(their order is what decides which one wins), that the shapes named are the
ones the design creates, and that the units are the units each family is
spelled in. The budget's own assertions are smaller and blunter — that it
points at the compartment, that somebody receives its alerts, and that its
amount holds the month the declared fleet costs at Oracle's list prices.
"""

import re
from collections.abc import Sequence

import pulumi
import pytest
import pytest_asyncio
from mock_monitor import Recorder, root_of, run_with, the_run_failed_with

from kluster import conventions
from kluster.components.cloud.guardrails import (
    A1_CORES_PER_AD,
    A1_MEMORY_GB_PER_AD,
    BUDGET_AMOUNT,
    OBJECT_STORAGE_GB,
    AlertRule,
    Guardrails,
    NoRecipient,
    quota_statements,
)
from kluster.lib.state_backend import settings as state_backend

TENANCY_ID = 'ocid1.tenancy.oc1..test'
COMPARTMENT_ID = 'ocid1.compartment.oc1..test'
COMPARTMENT_NAME = 'kluster'
RECIPIENTS = ('alerts@example.invalid', 'second@example.invalid')

#: What the `physical` stack hands the component: one alert on actual spend and
#: one on the forecast, which is the pair the assertions below are about. The
#: roll is the caller's, so this suite states its own.
ALERT_RULES: tuple[AlertRule, ...] = (
    AlertRule(name='full', threshold=100, message='The monthly cloud budget is spent.'),
    AlertRule(name='forecast', threshold=100, message='This month is forecast to end over budget.', forecast=True),
)


@pytest_asyncio.fixture(autouse=True)
async def monitor() -> Recorder:
    return await run_with(Recorder(), stack='physical')


def build(recipients: pulumi.Input[Sequence[str]] = RECIPIENTS, name: str = 'kluster') -> Guardrails:
    return Guardrails(
        name,
        tenancy_id=TENANCY_ID,
        compartment_id=COMPARTMENT_ID,
        compartment_name=COMPARTMENT_NAME,
        recipients=recipients,
        alert_rules=ALERT_RULES,
    )


def test_compute_is_denied_before_the_one_shape_is_allowed() -> None:
    lines = quota_statements(COMPARTMENT_NAME)
    zeroed = [index for index, line in enumerate(lines) if line.startswith('zero compute')]
    allowed = [index for index, line in enumerate(lines) if 'standard-a1' in line]

    assert zeroed and allowed
    # A later statement supersedes an earlier one for the same quota, so a
    # wildcard `zero` placed after the `set` would silently forbid the fleet.
    assert max(zeroed) < min(allowed)


def test_both_halves_of_a_flexible_shape_are_capped() -> None:
    lines = quota_statements(COMPARTMENT_NAME)
    # Cores and memory are separate families on a flexible shape; capping only
    # cores leaves the memory dimension open.
    assert f'set compute-core quota standard-a1-core-count to {A1_CORES_PER_AD} ' in ' '.join(lines) + ' '
    assert f'set compute-memory quota standard-a1-memory-count to {A1_MEMORY_GB_PER_AD} ' in ' '.join(lines) + ' '


#: The compartment quotas that govern an A1 shape, as the Compute API lists them
#: in the shape's `quotaNames` (ListShapes; Oracle's CLI walkthrough for Oracle
#: Solaris on OCI records the list for `BM.Standard.A1.160`). The service's
#: list, not this program's: a quota on it that the policy zeroes and never
#: sets refuses every A1 instance.
A1_SHAPE_QUOTA_NAMES = (
    'standard-a1-memory-count',
    'standard-a1-memory-regional-count',
    'standard-a1-core-count',
    'standard-a1-core-regional-count',
)

_SET = re.compile(r'set (?P<family>\S+) quota (?P<quota>\S+) to (?P<value>\d+) in ')


def quota_values(lines: Sequence[str]) -> dict[str, int]:
    """Each quota a `set` statement names, with the value the last such statement gives it."""
    return {match['quota']: int(match['value']) for line in lines if (match := _SET.match(line))}


def test_every_quota_on_the_a1_shape_is_set_after_the_deny() -> None:
    lines = quota_statements(COMPARTMENT_NAME)
    last_zero = max(index for index, line in enumerate(lines) if line.startswith('zero compute'))
    after = quota_values(lines[last_zero + 1 :])
    # The wildcard holds every one of them at 0, and the instance is refused
    # by whichever is left there, per AD or per region.
    assert set(A1_SHAPE_QUOTA_NAMES) <= set(after)


def admitted(cores: int, memory_gb: int, *, quotas: dict[str, int], scope: str) -> bool:
    suffix = '-regional-count' if scope == 'region' else '-count'
    return cores <= quotas[f'standard-a1-core{suffix}'] and memory_gb <= quotas[f'standard-a1-memory{suffix}']


def test_the_region_admits_the_fleet_and_one_replacement_in_flight() -> None:
    quotas = quota_values(quota_statements(COMPARTMENT_NAME))
    # A replacement is created before the node it replaces is deleted, so the
    # region holds every declared node and the new one at once.
    nodes = len(conventions.CLOUD_NODES) + 1
    assert admitted(nodes * conventions.NODE_OCPUS, nodes * conventions.NODE_MEMORY_GB, quotas=quotas, scope='region')


def test_an_ad_admits_its_node_beside_a_replacement() -> None:
    quotas = quota_values(quota_statements(COMPARTMENT_NAME))
    # One node per AD, and the replacement lands beside it.
    assert admitted(2 * conventions.NODE_OCPUS, 2 * conventions.NODE_MEMORY_GB, quotas=quotas, scope='ad')


def test_the_region_bounds_what_the_ads_together_would_admit() -> None:
    quotas = quota_values(quota_statements(COMPARTMENT_NAME))
    # One AD per node is the most the fleet spreads over, and the per-AD room
    # summed across them is a fleet's worth of replacements. The regional
    # quota is the bound that holds the compartment to one.
    ads = len(conventions.CLOUD_NODES)
    per_ad = (quotas['standard-a1-core-count'], quotas['standard-a1-memory-count'])
    assert not admitted(ads * per_ad[0], ads * per_ad[1], quotas=quotas, scope='region')


def test_the_region_refuses_a_second_replacement_in_flight() -> None:
    quotas = quota_values(quota_statements(COMPARTMENT_NAME))
    # Never more than one node created at once: the capacity hold rests on
    # it, and it is the regional quota alone that enforces it.
    nodes = len(conventions.CLOUD_NODES) + 2
    assert not admitted(
        nodes * conventions.NODE_OCPUS, nodes * conventions.NODE_MEMORY_GB, quotas=quotas, scope='region'
    )


#: Oracle's list prices and free allowances, the independent source the budget
#: is held against: A1 per OCPU-hour and per GB-hour past the allowance every
#: tenancy gets (the Always Free resources page), and block storage per
#: GB-month past its 200 GB, priced at the Lower Cost tier (VPU 0) as
#: nodes.md §3.2 prices that overage.
A1_PER_OCPU_HOUR = 0.01
A1_PER_GB_HOUR = 0.0015
A1_FREE_OCPU_HOURS = 1500
A1_FREE_GB_HOURS = 9000
BLOCK_PER_GB_MONTH = 0.0255
BLOCK_FREE_GB = 200

#: A 31-day month, the longest a monthly budget has to cover.
LONGEST_MONTH_HOURS = 744


def test_the_budget_holds_the_month_the_fleet_declares() -> None:
    # The forecast alert fires on a month projected past the amount, so an
    # amount below what the declared fleet costs alerts on every month, and
    # one that stopped following a resized fleet is caught here.
    nodes = len(conventions.CLOUD_NODES)
    ocpu_hours = nodes * conventions.NODE_OCPUS * LONGEST_MONTH_HOURS
    gb_hours = nodes * conventions.NODE_MEMORY_GB * LONGEST_MONTH_HOURS
    # The whole tenancy's block storage: the allowance is the tenancy's, so
    # the overage is counted entire, whichever compartment it bills to.
    block_gb = (
        nodes * conventions.NODE_BOOT_VOLUME_GB
        + sum(entry.size_gb for entry in conventions.NODE_VOLUMES.values())
        + state_backend.BOOT_VOLUME_GB
    )
    month = (
        max(0, ocpu_hours - A1_FREE_OCPU_HOURS) * A1_PER_OCPU_HOUR
        + max(0, gb_hours - A1_FREE_GB_HOURS) * A1_PER_GB_HOUR
        + max(0, block_gb - BLOCK_FREE_GB) * BLOCK_PER_GB_MONTH
    )
    assert month > 0  # not vacuous: the fleet bills past the allowance
    assert month <= BUDGET_AMOUNT


def test_object_storage_is_capped_in_the_unit_it_is_spelled_in() -> None:
    lines = quota_statements(COMPARTMENT_NAME)
    storage = [line for line in lines if 'object-storage' in line]
    assert len(storage) == 1
    # The quota is `storage-bytes`; handing it a number of gigabytes would cap
    # the bucket at a quarter of a kilobyte.
    assert f'to {OBJECT_STORAGE_GB * 1000**3} ' in storage[0]


def test_every_statement_names_the_compartment() -> None:
    lines = quota_statements(COMPARTMENT_NAME)
    # A policy that lost its statements would satisfy the loop.
    assert lines
    for line in lines:
        # The statement language has no OCIDs: a compartment is named, and a
        # statement that names none applies to the whole tenancy.
        assert line.endswith(f'in compartment {COMPARTMENT_NAME}')


@pytest.mark.asyncio
async def test_the_quota_and_budget_live_above_what_they_govern() -> None:
    guardrails = build()
    assert await guardrails.quota.compartment_id.future() == TENANCY_ID
    assert await guardrails.budget.compartment_id.future() == TENANCY_ID
    # The budget names its subject by OCID, which is the only place in this
    # component where the compartment appears as an id rather than a name.
    assert await guardrails.budget.targets.future() == [COMPARTMENT_ID]
    assert await guardrails.budget.target_type.future() == 'COMPARTMENT'


@pytest.mark.asyncio
async def test_one_alert_can_arrive_before_the_money_is_spent() -> None:
    guardrails = build()
    types = {name: await rule.type.future() for name, rule in guardrails.alerts.items()}
    # An alert that only reports actual spend always arrives after the fact;
    # the forecast rule is the one with time left in it.
    assert 'FORECAST' in types.values()
    assert 'ACTUAL' in types.values()


@pytest.mark.asyncio
async def test_every_alert_has_an_audience() -> None:
    guardrails = build()
    # "Every alert" is a claim about the roll, so the roll is pinned first.
    assert set(guardrails.alerts) == {rule.name for rule in ALERT_RULES}
    for rule in guardrails.alerts.values():
        assert await rule.recipients.future() == ','.join(RECIPIENTS)
        assert await rule.threshold_type.future() == 'PERCENTAGE'


@pytest.mark.asyncio
async def test_secret_addresses_reach_every_alert_as_a_secret() -> None:
    """The addresses are private, so a caller holding them as a secret gets a secret on every rule."""
    guardrails = build(pulumi.Output.secret(RECIPIENTS))
    assert guardrails.alerts
    for rule in guardrails.alerts.values():
        assert await rule.recipients.is_secret()
        assert await rule.recipients.future() == ','.join(RECIPIENTS)


@pytest.mark.asyncio
async def test_a_budget_nobody_hears_is_refused() -> None:
    # The list is in hand only once the input resolves, so that is where the
    # refusal is: in the audience every rule receives.
    guardrails = build((), name='kluster-silent')
    assert guardrails.alerts
    budget = await guardrails.budget.display_name.future()
    refusals: list[NoRecipient] = []
    for rule in guardrails.alerts.values():
        with pytest.raises(ValueError) as failed:
            await rule.recipients.future()
        # The output fails with the SDK's exception around the program's own.
        refusal = root_of(failed.value, NoRecipient)
        assert refusal.budget == budget
        refusals.append(refusal)
    # And the run fails on it, as the deployment would: the rule's own
    # registration meets the same refusal.
    assert await the_run_failed_with(NoRecipient) in refusals
