"""What this program calls itself, and the names three packages have to agree on."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from types import MappingProxyType

CLUSTER_NAME = 'kluster'

#: The state-backend appliance (physical/state-backend.md), which is one name
#: wherever the appliance is named: the operator stack that declares it
#: (`STACK_NAMES.state_backend`), the prefix on every cloud resource the box
#: owns, the IAM principal its provisioner signs as, the workstation slot that
#: key lands in, and the `credentials derived` rows that mint or draw what the
#: box runs on (`oci-state-backend`, `state-backend-server`, ...). A name
#: several packages have to agree on is a convention, not a setting of any one
#: of them.
STATE_BACKEND = 'state-backend'

#: The stack that owns the cloud installation (declarative/physical.md), which
#: is likewise one name in three places: the stack itself
#: (`STACK_NAMES.physical`), the IAM principal it signs as, and the compartment
#: that principal administers.
PHYSICAL = 'physical'


@dataclass(frozen=True)
class StackNames:
    """Every stack of this project, by the name `pulumi stack select` takes (declarative/README.md §1).

    A census two programs read. The Pulumi program dispatches on it
    (`kluster.stacks.STACKS`) and names the stack a StackReference reaches
    into by it; the `credentials` command refuses a delivery aimed at a name
    outside it, and names by it the stacks whose configuration is encrypted
    under a passphrase of their own (`scripts.credentials.pulumi_config`). A
    script may import no stack program, so the dispatch table cannot be the
    census itself.

    One field per stack. A new stack is a field here and a program in the
    dispatch table, and `test_conventions` holds the two to the same set.
    """

    physical: str
    dns: str
    k8s_base: str
    apps: str
    github: str
    state_backend: str

    def names(self) -> tuple[str, ...]:
        """Every stack name, in declaration order."""
        return tuple(value for value in vars(self).values() if isinstance(value, str))


STACK_NAMES = StackNames(
    physical=PHYSICAL, dns='dns', k8s_base='k8s-base', apps='apps', github='github', state_backend=STATE_BACKEND
)


class StateHome(Enum):
    """Where an operator stack keeps its state (framework/pulumi.md §3.3)."""

    #: The appliance's Postgres, where every stack CI deploys keeps its state
    #: too (framework/ci.md §1). A run reaches it with the `operator` client
    #: bundle.
    BACKEND = 'backend'
    #: A checkpoint committed to this repository under `checkpoints/`, for a
    #: stack whose state cannot live in the backend it creates. A run of one
    #: is checked before it starts and before it can be pushed.
    COMMITTED = 'committed'


#: Every **operator stack** -- a stack no CI job runs -- and where its state
#: lives. The census the rest reads: the passphrase that encrypts these
#: stacks apart from the others covers exactly these
#: (`scripts.credentials.pulumi_config.APART`), the census over the workflows
#: keeps every one of them out of CI (`test_conventions`), and the
#: `operator-stack` driver runs these and nothing else, with a backend chosen
#: by the home recorded here (`kluster.lib.stack_environment`).
#:
#: `state-backend` is the one whose state is committed: rfc-006 §4 declares the
#: appliance in it, the backend every other stack keeps its state in, which
#: must exist before Pulumi can act (framework/ci.md §1), so its own state
#: cannot live there. Its program raises until that declaration is written.
OPERATOR_STACKS: Mapping[str, StateHome] = MappingProxyType(
    {STACK_NAMES.github: StateHome.BACKEND, STACK_NAMES.state_backend: StateHome.COMMITTED}
)

#: The unattended rebuild drill (physical/state-backend.md §7.3), which is
#: one name in four places: the ops repository's Environment its credentials
#: land in (`forge.DRILL`), the IAM principal it signs as, the compartment
#: that principal administers, and the B2 key it reads the dumps with. Not a
#: stack and not a command of this repository -- a workflow in the ops
#: repository -- which is why its rows carry the name as a prefix on every
#: secret (credentials.md §3).
DRILL = 'drill'

#: Prefix for every label/annotation key this program owns. A k8s label key
#: prefix must be a DNS subdomain; this one is a zone we control, so the keys
#: can never collide with an upstream chart's.
LABEL_DOMAIN = 'kluster.ucw.phd'
