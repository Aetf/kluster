# Physical Design

System designs of the physical layer — the machines and appliances
themselves: how each box is built, configured, updated, monitored, and
operated. Parallel to [cluster/](../cluster/) (the cluster design) and
distinct from [declarative/physical.md](../declarative/physical.md)
(how these systems are *declared* in the Pulumi program).

**The boundary rule** (2026-08-24): `cluster/` states the cluster's
*requirements on* the physical layer — capacity, placement,
selection rationale, what a machine must provide (nodes.md's sizing
and inventory are the model); `physical/` owns *how the machine
delivers it* — OS, disk/network shape, lifecycle, playbooks. The
test for misplaced content: **would this paragraph change if the
cluster design changed?** If yes, it's a requirement and belongs in
`cluster/`; if it would only change because the machine changed, it
belongs here.

Documents:

-   **[state-backend.md](state-backend.md)** — the Pulumi
    state-backend appliance (FCOS on the OCI E2.1.Micro): config
    management (re-provision as the only apply path), Postgres
    lifecycle, PKI, NSG posture, backup with generational age keys,
    monitoring, playbook census.
-   **[homelab-host.md](homelab-host.md)** — the homelab host & worker
    VM: disk shape (nodatacow raw + virtio-blk), the second host
    bridge, two-phase GPU passthrough, the host-prep change-set.
-   **[gateway.md](gateway.md)** — the UDM as a system: the ZeroTier
    network design (roster, routes, CI-confining flow rules, cutover
    order), recovery playbooks, firewall target state (pending).
