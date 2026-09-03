"""What stops a mistake from becoming a bill (nodes.md §3.2).

The tenancy is Pay-As-You-Go, and **there is no hard spending cap**. The
failure mode of a pay-as-you-go tenancy is inverted from a free one — a limits
change or a fat-fingered shape gets *billed* instead of *killed*, which is the
survivable half of the trade but only if something is watching. Two mechanisms
watch, and only one of them can actually refuse anything:

-   **Compartment quotas** are resource-side and enforced at creation time: a
    request for a shape outside the declared envelope fails. This is the real
    guardrail.
-   **A budget with alert rules** is a notification, evaluated roughly daily.
    It cannot stop spending; it reports that spending happened. It exists for
    the costs quotas cannot express — egress, request counts, a price change.

**Quota scope is per availability domain** for compute and block storage. The
fleet is one node per AD (nodes.md §5), so the per-AD numbers below describe
one node's worth of headroom, and the tenancy-wide ceiling is that multiplied
by the number of ADs in the region. That is a looser bound than the free
envelope, and deliberately so: a bound tight enough to forbid a second node in
an AD would also forbid replacing one, which is the operation the design
depends on being available (nodes.md §3.1, the capacity hunt).

**Statement order is load-bearing.** Within one policy, a later statement
supersedes an earlier one for the same quota, so the wildcard `zero` comes
first and the shapes the design actually creates are set after it. The effect
is a default-deny on compute: every shape family is unavailable except the one
the cluster runs on.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import pulumi
import pulumi_oci as oci

from putils import Component

#: Per AD. One node is 1 OCPU / 8 GB; the second core is the recorded headroom
#: for the node carrying the cache workload if it runs hot (nodes.md §3.2).
A1_CORES_PER_AD = 2
A1_MEMORY_GB_PER_AD = 16

#: Per AD. A node's boot volume is 50 GB and a node may carry one attached
#: volume, the largest of which is 110 GB (`conventions.NODE_VOLUMES`); the
#: remainder is room to restore such a volume beside the one it replaces. A
#: bound that fitted only today's smaller volume would refuse to create the
#: larger one at all.
BLOCK_STORAGE_GB_PER_AD = 270

#: The free allowance is five volume backups, and nothing in the design takes
#: them — durability is backup/restore through the object store (storage.md §5).
VOLUME_BACKUPS = 5

#: Regional. This installation declares no bucket in this compartment — its
#: one object bucket is on the other provider on purpose (storage.md §4) — so
#: this caps what the tenancy stores here on the installation's behalf. It is
#: not zeroed the way the compute families are: a custom image import is not
#: clearly outside this family, and a quota that refused the machine image
#: would refuse the fleet.
OBJECT_STORAGE_GB = 250

#: A month's spend the design already accepts: the A1 fleet under the
#: conservative half of the free allowance, plus the block storage past 200 GB
#: (nodes.md §3.2). Alerts fire against this, in whole units of the tenancy's
#: currency.
BUDGET_AMOUNT = 25


@dataclass(frozen=True)
class AlertRule:
    """One budget alert: a threshold, and whether it looks at spend or trend."""

    name: str
    threshold: float
    """Percentage of the budget amount at which the alert fires."""
    message: str
    forecast: bool = False
    """Whether the trigger is the projected month-end spend rather than actual spend."""

    @property
    def type(self) -> str:
        return 'FORECAST' if self.forecast else 'ACTUAL'


#: Three rules, each answering a different question. Half-way through the
#: month is "something changed"; at the budget is "it already happened"; the
#: forecast is the only one that can arrive while there is still time to act.
ALERT_RULES: tuple[AlertRule, ...] = (
    AlertRule(name='half', threshold=50, message='Half of the monthly cloud budget is spent.'),
    AlertRule(name='full', threshold=100, message='The monthly cloud budget is spent.'),
    AlertRule(
        name='forecast',
        threshold=100,
        message='This month is forecast to end over the cloud budget.',
        forecast=True,
    ),
)


class Guardrails(Component):
    """The compartment's quota policy, its budget, and the budget's alerts.

    Both resources are declared in the tenancy's root compartment because OCI
    requires it: a quota policy and a budget are tenancy-level objects that
    *name* the compartment they act on rather than living inside it. The
    compartment is therefore needed twice — by OCID for the budget's target,
    and by name for the quota statements, whose language has no OCIDs.
    """

    def __init__(
        self,
        name: str,
        *,
        tenancy_id: pulumi.Input[str],
        compartment_id: pulumi.Input[str],
        compartment_name: str,
        recipients: Sequence[str],
        budget_amount: int = BUDGET_AMOUNT,
        alert_rules: Sequence[AlertRule] = ALERT_RULES,
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(name, opts=opts)
        if not recipients:
            raise ValueError('a budget alert with no recipient notifies nobody')

        self.statements = quota_statements(compartment_name)

        self.quota = oci.limits.Quota(
            f'{name}-quota',
            compartment_id=tenancy_id,
            name=f'{name}-envelope',
            description='Creatable shapes, pinned to the envelope the design is budgeted against',
            statements=self.statements,
            opts=self.child_opts(),
        )

        self.budget = oci.budget.Budget(
            f'{name}-budget',
            compartment_id=tenancy_id,
            display_name=f'{name}-budget',
            description='Monthly spend on the cluster compartment',
            amount=budget_amount,
            reset_period='MONTHLY',
            target_type='COMPARTMENT',
            targets=[compartment_id],
            opts=self.child_opts(),
        )

        self.alerts = {
            rule.name: oci.budget.Rule(
                f'{name}-budget-{rule.name}',
                budget_id=self.budget.id,
                display_name=f'{name}-{rule.name}',
                description=rule.message,
                message=rule.message,
                threshold=rule.threshold,
                threshold_type='PERCENTAGE',
                type=rule.type,
                # OCI takes the audience as one comma-separated string.
                recipients=','.join(recipients),
                opts=self.child_opts(),
            )
            for rule in alert_rules
        }

        self.register_outputs({})


def quota_statements(
    compartment: str,
    *,
    a1_cores: int = A1_CORES_PER_AD,
    a1_memory_gb: int = A1_MEMORY_GB_PER_AD,
    block_storage_gb: int = BLOCK_STORAGE_GB_PER_AD,
    volume_backups: int = VOLUME_BACKUPS,
    object_storage_gb: int = OBJECT_STORAGE_GB,
) -> list[str]:
    """The quota policy, in OCI's statement language.

    Only families whose names are verified against the service's own quota
    reference appear here. A statement naming a family that does not exist is
    rejected outright, so an unverified guess would cost the whole policy —
    including the parts that do work.

    `compartment` is a name, not an OCID: unlike IAM policy, the quota
    statement language has no OCID form, and a nested compartment is written
    as the path `parent:child`.
    """
    return [
        # Default-deny, then the exceptions. `/*/` is the wildcard form the
        # statement language uses for "every quota in this family".
        f'zero compute-core quotas /*/ in compartment {compartment}',
        f'zero compute-memory quotas /*/ in compartment {compartment}',
        f'set compute-core quota standard-a1-core-count to {a1_cores} in compartment {compartment}',
        f'set compute-memory quota standard-a1-memory-count to {a1_memory_gb} in compartment {compartment}',
        f'set block-storage quota total-storage-gb to {block_storage_gb} in compartment {compartment}',
        f'set block-storage quota backup-count to {volume_backups} in compartment {compartment}',
        # Object Storage bills by the byte, and its quota is spelled that way.
        f'set object-storage quota storage-bytes to {object_storage_gb * 1000**3} in compartment {compartment}',
    ]
