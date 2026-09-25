"""What this program calls itself, and the names three packages have to agree on."""

from __future__ import annotations

from dataclasses import dataclass

CLUSTER_NAME = 'kluster'

#: The state-backend appliance (physical/state-backend.md), which is one name
#: in four places: the prefix on every cloud resource the box owns, the IAM
#: principal its provisioner signs as, the workstation slot that key lands in,
#: and the `credentials derived oci` subcommand that mints it. A name three
#: packages have to agree on is a convention, not a setting of any one of them.
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

    def names(self) -> tuple[str, ...]:
        """Every stack name, in declaration order."""
        return tuple(value for value in vars(self).values() if isinstance(value, str))


STACK_NAMES = StackNames(physical=PHYSICAL, dns='dns', k8s_base='k8s-base', apps='apps', github='github')

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
