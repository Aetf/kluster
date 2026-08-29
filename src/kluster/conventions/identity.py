"""What this program calls itself, and the names three packages have to agree on."""

from __future__ import annotations

CLUSTER_NAME = 'kluster'

#: The state-backend appliance (physical/state-backend.md), which is one name
#: in four places: the prefix on every cloud resource the box owns, the IAM
#: principal its provisioner signs as, the workstation slot that key lands in,
#: and the `credentials derived oci` subcommand that mints it. A name three
#: packages have to agree on is a convention, not a setting of any one of them.
STATE_BACKEND = 'state-backend'

#: The stack that owns the cloud estate (declarative/physical.md), which is
#: likewise one name in three places: the stack itself, the IAM principal it
#: signs as, and the compartment that principal administers.
PHYSICAL = 'physical'

#: Prefix for every label/annotation key this program owns. A k8s label key
#: prefix must be a DNS subdomain; this one is a zone we control, so the keys
#: can never collide with an upstream chart's.
LABEL_DOMAIN = 'kluster.ucw.phd'
