"""The spending guardrails.

A quota policy is a text document the service parses, so the things worth
asserting are textual: that the default-deny statement precedes the exceptions
(their order is what decides which one wins), that the shapes named are the
ones the design creates, and that the units are the units each family is
spelled in. The budget's own assertion is smaller and blunter — that it points
at the compartment and that somebody receives its alerts.
"""

import pytest
import pytest_asyncio
from mock_monitor import Recorder, run_with

from kluster.components.cloud.guardrails import (
    A1_CORES_PER_AD,
    A1_MEMORY_GB_PER_AD,
    BUDGET_AMOUNT,
    OBJECT_STORAGE_GB,
    AlertRule,
    Guardrails,
    quota_statements,
)

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


def build() -> Guardrails:
    return Guardrails(
        'kluster',
        tenancy_id=TENANCY_ID,
        compartment_id=COMPARTMENT_ID,
        compartment_name=COMPARTMENT_NAME,
        recipients=RECIPIENTS,
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


def test_object_storage_is_capped_in_the_unit_it_is_spelled_in() -> None:
    lines = quota_statements(COMPARTMENT_NAME)
    storage = [line for line in lines if 'object-storage' in line]
    assert len(storage) == 1
    # The quota is `storage-bytes`; handing it a number of gigabytes would cap
    # the bucket at a quarter of a kilobyte.
    assert f'to {OBJECT_STORAGE_GB * 1000**3} ' in storage[0]


def test_every_statement_names_the_compartment() -> None:
    from kluster.components.cloud.guardrails import quota_statements

    for line in quota_statements(COMPARTMENT_NAME):
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
async def test_the_budget_resets_monthly() -> None:
    guardrails = build()
    assert await guardrails.budget.reset_period.future() == 'MONTHLY'
    assert await guardrails.budget.amount.future() == BUDGET_AMOUNT


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
    for rule in guardrails.alerts.values():
        assert await rule.recipients.future() == ','.join(RECIPIENTS)
        assert await rule.threshold_type.future() == 'PERCENTAGE'


def test_a_budget_nobody_hears_is_refused() -> None:
    with pytest.raises(ValueError, match='notifies nobody'):
        Guardrails(
            'kluster-silent',
            tenancy_id=TENANCY_ID,
            compartment_id=COMPARTMENT_ID,
            compartment_name=COMPARTMENT_NAME,
            recipients=(),
            alert_rules=ALERT_RULES,
        )
