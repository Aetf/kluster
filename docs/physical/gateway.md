# Gateway (UDM)

The UDM-SE as a system: the home site's router, firewall, ZeroTier
terminator, and host of the nspawn estate. This document owns *how the
machine delivers* what the cluster design demands of it; the demands
themselves live in cluster/ (BGP peering and the `lan` pool —
architecture.md §3.4; full desired-state absorption — §5.2; ZT
termination — §5.3) and the declaration mechanics in
declarative/physical.md §4.

## 1. Roles on the box

-   **Routing**: inter-VLAN routing for the three home VLANs
    (br0 = server LAN 192.168.80.0/24, br2 = IoT 192.168.90.0/24,
    br5 = container VLAN 10.0.5.0/24), BGP peer for the `lan` pool
    (192.168.70.0/24 + ULA /64) learned from the homelab worker VM.
-   **nspawn estate** (units + digest-pinned rootfs pushed by the
    gw-config provider): caddy, AdGuard ×2 (alice/bob), and the
    **ZeroTier member container** (§2) as the fourth member.
-   **Zone firewall**: UBIOS zone-based firewall, declared through the
    bridged filipowm/unifi provider (architecture.md §5.1). Target
    state: §4.
-   **ZeroTier router**: the home side's ZT terminator (§2) — a
    net-new role; today ZT and the home LANs are not connected at
    all (no member routes anything).

## 2. ZeroTier network design

Architecture.md §5.3 decides *where* ZT terminates (the UDM) and *what
governs it* (ZT Central config in the `physical` stack via the bridged
`zerotier/zerotier` provider). This section is the network-level
design: roster, addressing, routes, flow rules, and cutover.

### 2.1 Member roster & addressing

Every member is declared as a `zerotier_member` resource with an
explicit static `ip_assignments` entry and a `role` tag — the Pulumi
roster *is* the census; a member without a declared tag cannot exist
in the desired state (relevant because the tag default is permissive,
§2.3).

| Member | role tag | Notes |
| --- | --- | --- |
| Personal devices (phones, laptops) | `personal` | Full access — parity with sitting on the LAN. |
| UDM | `infra` | Static managed IP (`conventions.py`); nexthop of every managed route. |
| Homelab host | `infra` | Today's only home member — a plain member, never a router (ZT carries no home-LAN routes today); kept as the recovery side-door (§3). |
| Legacy VPS | `infra` | Retires in Wave F together with its `10.42.0.0/24` route. |
| CI ephemeral member | `ci` | Identity generated in-state (`zerotier_identity`), private key in the CI environment secret; confined by §2.3. |

Dead weight dropped at import census: the Abacus member (machine no
longer exists). DNS linkage: the `*.zt.<zone>` block in the `dns`
stack (dns.md §2) mirrors this roster — a `udm.zt` record is added,
`abacus.zt` dropped, and the VPS's record retires with it in Wave F.
Roster and records change in the same review since both live in Pulumi.

### 2.2 Managed routes

All LAN reachability via the UDM member's ZT address, one route per
subnet: 192.168.80.0/24, 192.168.90.0/24, 10.0.5.0/24, and the `lan`
pool 192.168.70.0/24 (+ its ULA /64) — the pool is reached through the
UDM's own BGP-learned route, one hop. The legacy `10.42.0.0/24`-via-VPS
route is deleted in Wave F. The homelab host advertises nothing.

### 2.3 Flow rules — confining the CI member

Facts about the rules engine that shape the draft (docs.zerotier.com
/rules; quirks from ZeroTierOne #2200):

-   **Evaluation is distributed and stateless**: every packet is
    evaluated independently at both sender and receiver; there is no
    connection tracking, so each allowed flow needs its **return-leg
    mirror rule** (dport on the outbound leg becomes sport on the
    reply).
-   **`tseq`/`treq`** match the *sender's* / *receiver's* tag value
    alone — the primitive for "this member is CI", with no dependence
    on the other end's tag (the bitwise matchers `tand`/`tor`/`txor`
    combine both ends' values and are wrong for this).
-   **Routed traffic keeps its pre-forward destination**: a packet for
    a LAN host rides ZT with ethernet dst = the UDM member but IP dst =
    the LAN address, so `ipdest` matches LAN CIDRs directly.
    (Confirmed by the engine model; still on the §2.4 checklist.)
-   **#2200 quirks, designed around**: when one end's tags are not yet
    known the evaluator force-matches tag rules (first packets may hit
    the CI drop until the credential exchange lands — a transient,
    retried by TCP; accepted); `not` combined with tag or IP/port
    matchers inverts missing-information zeros and misfires across
    address families — **the draft uses positive matches only** (the
    stock ethertype base filter is the sole exception, it predates and
    survives the quirk); ARP is accepted early so it never reaches the
    IP/tag matchers.

Draft (`flow_rules` string on the `zerotier_network` resource; IP and
port literals come from `conventions.py`):

```text
tag role
  id 1000
  default 0        # personal — see roster discipline in §2.1
  enum 0 personal
  enum 1 infra
  enum 2 ci
;

# stock base filter: IP + ARP only
drop
  not ethertype ipv4
  and not ethertype arp
  and not ethertype ipv6
;
accept ethertype arp;

# CI confinement: three targets, each flow as outbound leg + return leg.
# Targets: UDM SSH (gw-config push), the AdGuard APIs (alice/bob),
# the homelab host's libvirt SSH.
accept tseq role 2 and ipdest <udm-zt-ip>/32      and dport 22;
accept treq role 2 and ipsrc <udm-zt-ip>/32      and sport 22;
accept tseq role 2 and ipdest <adguard-addrs>    and dport <adguard-api>;
accept treq role 2 and ipsrc <adguard-addrs>     and sport <adguard-api>;
accept tseq role 2 and ipdest <homelab-host>/32  and dport 22;
accept treq role 2 and ipsrc <homelab-host>/32   and sport 22;
drop tseq role 2;
drop treq role 2;

# personal + infra: unrestricted (LAN-posture parity)
accept;
```

The `drop treq role 2` line also means nothing may *initiate* toward a
CI member — it is a client only. The permissive `default 0` is why the
roster discipline in §2.1 exists: an undeclared member would default to
`personal`, but membership itself is Pulumi-gated (a member the roster
doesn't authorize never joins), so the default is unreachable in
practice.

**Personal traffic and local discovery are untouched.** ZT is also
the personal devices' network segment, so the rules must not break
LAN-style behavior between them — and they don't: every rule above
matches only `ci`-tagged endpoints; all other traffic falls through
to the final `accept`. Multicast discovery (mDNS to `224.0.0.251` /
`ff02::fb`, SSDP) and IPv4 broadcast ride ethertype ipv4/ipv6, pass
the base filter, and reach the final accept like any unicast. What
discovery *does* depend on, declared rather than assumed:

-   **Network multicast settings** are explicit fields on the
    `zerotier_network` resource: broadcast enabled, and
    `multicast_limit` ≥ the roster size (the default 32 is ample
    today; the constraint is recorded so roster growth cannot
    silently break discovery).
-   **The CI member stays IPv4-only**: its `drop` pair would eat its
    own ICMPv6 neighbor discovery if it ever received a v6
    assignment — a constraint on the roster entry, not a rule
    change.
-   The #2200 first-packet transient (above) applies to any member
    pair until tags are exchanged, multicast included; mDNS/SSDP
    re-announce periodically, so discovery self-heals.

**Boundary fact**: discovery across the ZT↔LAN boundary does not
work and never did — link-local multicast does not cross a routed
hop, and the new managed routes carry unicast only. A ZT device
discovers other ZT members, not LAN devices. If that is ever wanted,
the shape is an mDNS reflector on the UDM spanning `zt*` and the
VLANs — deliberately not designed in.

These Central rules are the **only policing layer** for ZT-forwarded
traffic — the UBIOS firewall does not classify `zt*` interfaces and
forwards them on default ACCEPT (architecture.md §5.3).

### 2.4 Verification (test network, before cutover)

Run against a scratch ZT network with the same rules and a throwaway
`ci`-tagged member:

1.  CI member reaches exactly its three targets (SSH banner / API
    response), including the **return leg** (rules are stateless — a
    working handshake proves both directions).
2.  CI member cannot ping or reach any other LAN address through the
    routes, and cannot reach a `personal` member directly.
3.  `ipdest` LAN-CIDR matching on routed (pre-forward) destinations
    behaves as modeled.
4.  First-packet behavior after a fresh join (the #2200 transient):
    connection succeeds on retry within normal client timeouts.
5.  Personal members are unaffected: full reachability, ARP/ND intact.
6.  Local discovery between two personal members over ZT (an mDNS
    query/response round trip) works with the rules applied —
    exercises the multicast settings and the final-accept fallthrough
    together.

### 2.5 Rollout order

There is no cycle to break: today ZT carries **no route to the home
LANs at all** (the host is a plain member; the only managed route is
the legacy `10.42.0.0/24` via the VPS). The LAN→UDM path exists before
and independently of ZT, so the first deployment is a LAN-side
operation (migration.md Phase 0):

1.  **UDM container deploys via an operator-local `physical` run** —
    Phase 0 is local-bootstrap by nature (the CI that would later do
    this is itself being built); the gw-config push SSHes to the UDM
    over the LAN, no ZT in the path. The UDM member is pre-authorized
    in the roster.
2.  **Managed routes are net-new additions** — the home-LAN and
    lan-pool routes via the UDM member appear where none existed;
    existing members gain reachability and lose nothing. No flip, no
    transition window.
3.  **CI's per-run ZT join becomes load-bearing only after §2.4
    passes** — until the flow rules and routes are verified,
    `physical` runs stay operator-local.

Each step is its own previewed `physical` change; rollback of any
step is removing what it added.

## 3. Failure & recovery (playbook census)

Per the census discipline (state-backend.md §7): title, trigger, gist —
executable form ships with the implementation.

-   **ZT container down on the UDM** — trigger: physical-stack CI runs
    fail to reach the UDM; personal devices lose LAN reachability. The
    repair tool (gw-config push) itself rides ZT, hence the side-door:
    connect to the **homelab host's direct ZT address** (member-to-
    member traffic needs no managed routes), hop to the LAN, SSH the
    UDM, restart the unit / rerun on_boot.d. If the host is also down:
    physical presence (LAN).
-   **Firmware update wiped the estate** — trigger: post-update, nspawn
    units gone. on_boot.d re-materializes the estate autonomously
    (architecture.md §5.2); verify ZT comes back last (it carries the
    management path). Fallback if host-networking nspawn misbehaves
    post-update: the unifios-utilities apt pattern (§5.3).
-   **UDM replaced** — trigger: hardware failure/RMA. Restore from the
    UniFi autobackup (the pull-direction yadm timer), re-run the
    gw-config provider for the estate, re-authorize the *new* UDM
    member identity in the roster (identity lives in `/data`, lost with
    the box), re-point the managed routes at it — a LAN-side operation
    like §2.5 (personal members' direct paths still work throughout).

## 4. Firewall target state

*(Pending — Gap 4: the zone-matrix target state, including whether
IoT→LAN tightens to default-drop as a deferred two-phase decision.)*
