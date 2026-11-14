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

**A1 is bounded twice, per availability domain and per region**, and an
instance is created only where both admit it. Both bounds leave room for one
node more than the fleet holds, because replacing a node is the operation the
design depends on being available (nodes.md §3.1, the capacity hunt), and a
replacement exists beside the node it replaces: Pulumi creates the replacing
resource before it deletes the old one, an order the nodes component leaves at
its default. A bound equal to the fleet would therefore refuse every
replacement while the old node still runs.

-   **Per AD**, the bound is two nodes' worth: the fleet is one node per AD
    (nodes.md §5), so the second is the room a replacement takes there, and a
    bound tight enough to forbid a second node in an AD would also forbid
    replacing the first. Block storage is scoped per AD the same way.
-   **Per region**, the bound is the fleet plus one node, the fleet being
    what `conventions.CLOUD_NODES`, `NODE_OCPUS` and `NODE_MEMORY_GB`
    declare. That is the compartment's ceiling: one replacement in
    flight at a time, which is all the design asks for (never create more than
    one node at once, nodes.md §3.2). The per-AD numbers multiplied by the
    region's ADs would admit more, and the regional quota is what stops it.

Both are looser than the free envelope, and deliberately so: the replacement
in flight is billed for as long as it runs beside the node it replaces.

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

from putils import Component, async_output, resolve

#: Per AD. One node is 1 OCPU / 8 GB; the second core is the recorded headroom
#: for the node carrying the cache workload if it runs hot (nodes.md §3.2).
A1_CORES_PER_AD = 2
A1_MEMORY_GB_PER_AD = 16

#: Regional. The fleet plus one node: three nodes of 1 OCPU / 8 GB
#: (`conventions.CLOUD_NODES`, `NODE_OCPUS`, `NODE_MEMORY_GB`) and the
#: replacement that runs beside one of them, because the replacement is created
#: before the node it replaces is deleted and a bound of the fleet alone would
#: refuse it. A literal like the others, not a number computed from those
#: conventions: a bound that followed the fleet's declaration would follow a
#: fat-fingered node count or size along with it, which is the mistake it is
#: here to refuse. The suite compares the two instead.
A1_CORES_REGIONAL = 4
A1_MEMORY_GB_REGIONAL = 32

#: Per AD. A node's boot volume is 50 GB and a node may carry one attached
#: volume of 55 GB (`conventions.NODE_VOLUMES`); the remainder is room to
#: restore such a volume beside the one it replaces, 50 + 55 + 55, and it holds
#: a replacement node's boot volume beside the old one as well.
BLOCK_STORAGE_GB_PER_AD = 160

#: The free allowance is five volume backups, and nothing in the design takes
#: them — durability is backup/restore through the object store (storage.md §5).
VOLUME_BACKUPS = 5

#: Regional. What this compartment's Object Storage holds is what this stack
#: stages through it on the way to an import -- today the cloud image's
#: archive, in the image bucket (`components/talos/image.py`) -- and nothing
#: the installation keeps: the backup bucket is on the other provider on
#: purpose (storage.md §4), and every other stack's buckets are in that
#: stack's own compartment or account. So this caps that staging and whatever
#: the tenancy stores here on the installation's behalf. It is not zeroed the
#: way the compute families are: a custom image import is not clearly outside
#: this family, and a quota that refused the machine image would refuse the
#: fleet.
OBJECT_STORAGE_GB = 250

#: A month's spend the design already accepts: the A1 fleet under the
#: conservative half of the free allowance, plus the block storage past 200 GB
#: (nodes.md §3.2). Alerts fire against this, in whole units of the tenancy's
#: currency. Not a parameter: there is one tenancy and no caller has a second
#: opinion about what its month costs.
BUDGET_AMOUNT = 25


class NoRecipient(ValueError):
    """A budget whose alerts have nobody to notify; `budget` is the budget's display name.

    One rule, so a program tells the refusal apart by the type alone, and a
    `ValueError`, so a caller that catches one is unaffected. The message,
    built from the field, is for a person.
    """

    def __init__(self, *, budget: str) -> None:
        super().__init__(f'the budget {budget} has no alert recipient, so its alerts notify nobody')
        self.budget: str = budget


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


class Guardrails(Component, pulumi_type='kluster:cloud:Guardrails'):
    """The compartment's quota policy, its budget, and the budget's alerts.

    Both resources are declared in the tenancy's root compartment because OCI
    requires it: a quota policy and a budget are tenancy-level objects that
    *name* the compartment they act on rather than living inside it. The
    compartment is therefore needed twice — by OCID for the budget's target,
    and by name for the quota statements, whose language has no OCIDs.

    `alert_rules` is the roll of alerts the budget carries, and it arrives from
    the caller: what is worth being told about spending is a decision of the
    installation rather than of the mechanism that reports it.

    `recipients` is an input, which may be an output, rather than a sequence
    alone, because the addresses are private: a caller that holds them as a
    secret output passes that output, and the one string every alert rule
    receives is joined from it through `resolve`, which keeps the secret flag.
    A list with nobody on it is refused when that string is built, which is
    when the addresses are first in hand.
    """

    def __init__(
        self,
        name: str,
        *,
        tenancy_id: pulumi.Input[str],
        compartment_id: pulumi.Input[str],
        compartment_name: str,
        recipients: pulumi.Input[Sequence[str]],
        alert_rules: Sequence[AlertRule],
        opts: pulumi.ResourceOptions | None = None,
    ) -> None:
        super().__init__(name, opts=opts)
        self._recipients = recipients

        self.statements = quota_statements(compartment_name)

        self.quota = oci.limits.Quota(
            f'{name}-quota',
            compartment_id=tenancy_id,
            name=f'{name}-envelope',
            description='Creatable shapes, pinned to the envelope the design is budgeted against',
            statements=self.statements,
            opts=self.child_opts(),
        )

        display_name = f'{name}-budget'
        self.budget = oci.budget.Budget(
            f'{name}-budget',
            compartment_id=tenancy_id,
            display_name=display_name,
            description='Monthly spend on the cluster compartment',
            amount=BUDGET_AMOUNT,
            # Monthly because `BUDGET_AMOUNT` is a month's accepted cost: the
            # period and the amount are one decision, and the alerts'
            # percentages are of the month's spend.
            reset_period='MONTHLY',
            target_type='COMPARTMENT',
            targets=[compartment_id],
            opts=self.child_opts(),
        )

        # OCI takes the audience as one comma-separated string, and every rule
        # takes the same one.
        audience = async_output(self._audience(display_name))
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
                recipients=audience,
                opts=self.child_opts(),
            )
            for rule in alert_rules
        }

        self.register_outputs({})

    async def _audience(self, budget: str) -> str:
        recipients = await resolve(self._recipients)
        if not recipients:
            raise NoRecipient(budget=budget)
        return ','.join(recipients)


def quota_statements(
    compartment: str,
    *,
    a1_cores: int = A1_CORES_PER_AD,
    a1_memory_gb: int = A1_MEMORY_GB_PER_AD,
    a1_cores_regional: int = A1_CORES_REGIONAL,
    a1_memory_gb_regional: int = A1_MEMORY_GB_REGIONAL,
    block_storage_gb: int = BLOCK_STORAGE_GB_PER_AD,
    volume_backups: int = VOLUME_BACKUPS,
    object_storage_gb: int = OBJECT_STORAGE_GB,
) -> list[str]:
    """The quota policy, in OCI's statement language.

    Only quota names the service itself states appear here. A statement naming
    a family or a quota that does not exist is rejected outright, so an
    unverified guess would cost the whole policy — including the parts that do
    work. The compute quota reference
    (https://docs.oracle.com/iaas/Content/Quotas/Concepts/resourcequotas_topic-Compute_Quotas.htm)
    states every name below except the two regional A1 quotas; those are
    stated by the Compute API, which lists, as an A1 shape's `quotaNames`,
    the compartment quotas that govern it: the per-AD pair and the regional
    pair. Each regional quota is set in the family of its per-AD twin, and the
    wildcard `zero` of that family holds it at 0 until it is.

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
        # The wildcard holds these at 0 as well, and a region at 0 admits no
        # A1 instance whatever the per-AD quotas say.
        f'set compute-core quota standard-a1-core-regional-count to {a1_cores_regional} in compartment {compartment}',
        f'set compute-memory quota standard-a1-memory-regional-count to {a1_memory_gb_regional} in compartment {compartment}',
        f'set block-storage quota total-storage-gb to {block_storage_gb} in compartment {compartment}',
        f'set block-storage quota backup-count to {volume_backups} in compartment {compartment}',
        # Object Storage bills by the byte, and its quota is spelled that way.
        f'set object-storage quota storage-bytes to {object_storage_gb * 1000**3} in compartment {compartment}',
    ]
