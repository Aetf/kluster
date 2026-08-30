# Gateway (UDM)

The UDM-SE as a system: the home site's router, firewall, ZeroTier
terminator, and host of the nspawn container services. This document owns *how the
machine delivers* what the cluster design demands of it; the demands
themselves live in cluster/ (BGP peering and the `lan` pool —
architecture.md §3.4; full desired-state absorption — §5.2; ZT
termination — §5.3) and the declaration mechanics in
declarative/physical.md §4.

## 1. Roles on the box

-   **Routing**: inter-VLAN routing for the home networks — br0 = the
    untagged server LAN 192.168.80.0/24, br2 = IoT 192.168.90.0/24,
    br5 = container VLAN 10.0.5.0/24, and the **cluster VLAN, id 7,
    192.168.70.0/24**, whose only residents are Talos nodes (static
    addresses, no DHCP server on it). The cluster VLAN is the one
    network object the cluster brings with it, declared through the
    unifi provider alongside the firewall rules
    (declarative/physical.md §4). On top of that, **BGP peer for the
    `lan` pool** (192.168.71.0/24 + its ULA /64), learned from the
    homelab worker VM at its VLAN-7 address.

    **Site addressing convention**: every network this box hosts
    sits inside 192.168.0.0/16 and takes its **third octet from the
    VLAN id, times ten** — VLAN 7 → 192.168.70.0/24, VLAN 9 → IoT's
    192.168.90.0/24 — with the network's /64 out of the site's ULA
    prefix repeating that same third octet. The container VLAN's
    10.0.5.0/24 is the single network outside the scheme. The `lan`
    pool's 192.168.71.0/24 is deliberately not a multiple of ten: it
    is no VLAN and never becomes a network object (§4.1), and
    numbering it next to the VLAN it is announced from keeps the pair
    legible at a glance.
-   **nspawn container services** (units + digest-pinned rootfs pushed
    by the gw-config provider): caddy, AdGuard ×2 (alice/bob), and the
    **ZeroTier member container** (§2) as the fourth. A pin is a whole
    image reference, `<repository>:<tag>@sha256:<digest>`: the runner
    pulls that manifest by its digest and hashes it against the pin,
    hashes every layer against the digest the manifest gives for it,
    and flattens the layers into one plain archive; the push unpacks
    that into a per-service directory tree, and the unit boots the tree
    with `systemd-nspawn --directory=`. Which repository publishes a
    given build stays this repository's decision — the stack checks
    each pin against it and refuses a mismatch by key, so the two
    resolvers cannot drift onto two different images. The tree is derived
    state the push replaces whole and never edits, so nothing worth
    keeping lives in it — a service's writable state is bind-mounted
    from `/data` instead, which is also what makes a rootfs bump a
    software swap rather than a new identity. Doing the pull and the
    flattening on the runner is deliberate: the device boots a
    directory rather than running a container engine, and its userland
    is the cut-down one a router ships, where `tar` can be relied on
    and little else can.

    **The marker beside each tree names the manifest digest**, which
    is the pin and not a checksum of the archive lying next to it. A
    reviewer running `sha256sum` on the device should expect the two
    to differ: what the marker asserts is provenance — these bytes
    came from that image — and the flattening is content-equivalent to
    `podman export` rather than byte-identical to anything.

    **Each unit states its own requirements**, and nothing chooses a
    start order for it: the three services on the container VLAN bind
    to that bridge's device unit and come after it, so a container is
    never started against a bridge that does not exist yet, and the
    ZeroTier member asserts `/dev/net/tun` rather than depending on it
    — nothing tags that node in `udev`, so its device unit can be
    loaded but never activated and a dependency on it would fail the
    service permanently. The assertion is what turns "the daemon
    logged that it cannot open the device and went to sleep" into a
    failed unit with a reason. No unit names another: caddy proxies to
    the resolvers at request time, not at start time, and the ZeroTier
    member carries the management session rather than the others'
    traffic.

    **The images are Alpine with s6-overlay, not systemd**, which
    decides what a unit may say. It boots what the image ships at
    `/sbin/init`; readiness is nspawn's own message rather than one
    the guest never sends; a member is stopped with `SIGKILL`,
    because s6 returns from anything gentler with supervisors still
    holding the control group against the next start; and whatever a
    member has to be told arrives as **environment variables in its
    PID 1**, which is the only channel its startup scripts read. The
    AdGuard pair take their address that way (address, router and the
    SLAAC token, read by the image's own network setup before the
    resolver it guards may start). Caddy does not: its image asks for
    a DHCP lease, and the lease is where its resolver comes from too,
    so the address the census holds for it is the address the design
    intends and not one the unit delivers — closing that gap is work
    in the image, which needs the AdGuard pair's network setup plus a
    resolver that does not depend on these services. The ZeroTier member
    is told nothing at all: it is host-networked, and what it needs of
    its unit is the tunnel device and the state directory that is its
    identity on the overlay.

    **Host-networked is a requirement rather than an accident**, and it
    is written here because it is easy to lose in a refactor: the
    daemon creates an interface when it joins and this box routes
    through that interface, and an interface created inside a private
    namespace is invisible to the router that has to use it — the
    member would join and route nothing. The declaration states it by
    giving that one service no address, since no address means no
    bridge and nspawn leaves a container with no bridge in the host's
    own namespace.
-   **Zone firewall**: UBIOS zone-based firewall, declared through the
    bridged filipowm/unifi provider (architecture.md §5.1). Target
    state: §4.
-   **ZeroTier router**: the home side's ZT terminator (§2) — a
    net-new role; today ZT and the home LANs are not connected at
    all (no member routes anything).
-   **Its own TLS issuer**: caddy serves the box's vhosts (the UniFi
    console, both AdGuard UIs) under public-zone names and issues their
    certificates itself over ACME DNS-01, rather than consuming
    cert-manager's — the gateway's TLS has to keep renewing while the
    cluster is down or mid-rebuild, and pushing certificates from the
    cluster into the device would invert that dependency
    (declarative/dns.md §4). The credential that buys it is a **third
    Cloudflare token, zone-scoped and minted from the Cloudflare seed**
    (credentials.md §3), delivered as a **gw-config device secret**
    beside the nspawn units and read by nothing else. It is separate
    from cert-manager's DNS-01 token on purpose: two issuers that must
    survive each other's outage do not share a credential, and the
    device holding one of them is the one machine the cluster cannot
    re-seal.

### 1.1 The recovery script, and what decides a restart

Everything the routing daemon and the containers need lives under
`/data`, the one directory a firmware update leaves alone: the root
filesystem archives and the trees unpacked from them, the unit files,
the configuration each container reads, the per-container writable
state, and one **recovery script** in `on_boot.d`. The script is the
whole mechanism for making any of it take effect, and deliberately the
only one: it runs at boot, when nothing else is present and this
program is not reachable, and it runs again after every deployment as
every pushed file's post-apply hook. The path that recovers a device is
therefore the path every apply exercises, so it cannot rot unnoticed.

What it guarantees, in that order: every declared unit whose unit file
*and* root filesystem tree have landed is installed and enabled; a unit
this program no longer declares is disabled, removed and its stamp
cleared, and only units of this program's own prefix are candidates;
the routing daemon's configuration is copied to where the daemon reads
it, outside `/data`, and reloaded; a service that owns its own
configuration is given an initial state, but only where it has none,
because the software behind it rewrites that file afterward; and a
service is restarted **only if a file that defines it changed**.

That last point is load-bearing, and it is why the health of a service
here is decided by *files* rather than by a probe. The **content stamp**
is a file beside each service's state holding a checksum over
everything that defines that service — its unit, the digest marker of
its root filesystem tree, and every configuration file it mounts. That
set of paths is the service's **stamped set**, and the stamp is the
record of what was last acted on; comparing the two is how the script
tells intended content from pushed content without asking the service
anything about itself. An unchanged stamp plus an active unit means
nothing to do. There is no health check inside and no monitor:
`Restart=always` handles a service that dies and systemd is what
notices, while the script decides only whether the *definition* moved.

The stamped set names the root filesystem's **digest marker** rather
than the tree, because walking a root filesystem to learn it has not
changed costs more than the restart it would save — and the marker
beside the *tree* rather than beside the archive, because the archive's
marker is written after the hook has already run, so a service waiting
on it would learn about a new root filesystem one deployment late. The
two marker-shaped files are not the same thing: a **digest marker** is
written by the push and records which published artifact a payload came
from, while a **content stamp** is written by this script and records
what it last acted on. The first is an input to the second.

**One service is restarted last, and that is a fact about the
deployment rather than about the machine.** The apply reaches the
device over ZeroTier, and the overlay daemon carrying that session is
one of the containers being updated, so restarting it severs the
session that asked for the restart. Three things make that safe.
Nothing restarts that did not change, without which every apply would
cut its own session. The service carrying the session converges last,
so everything else has settled before the risk is taken — expressed as
the order of the restart loop, never as a unit ordering, which would
state something false about how the device boots. And an apply that
dies there fails its own resource: every step is idempotent and that
service's stamp has not been written yet, so the retry finds the work
already done. During a first bring-up there is no overlay to sever —
the container that will carry it is the thing being delivered — and the
session runs over the device's LAN address instead (§2.5).

### 1.2 The persistence mechanism: the boot chain and the custom root

Under everything above sits the mechanism that makes any of it survive a
firmware update, and it is declared by this program rather than pushed
by hand. It occupies two roots:

-   **`/data/on_boot.d`**, whose path is not a choice made here: it is
    compiled into the `ExecStart` of `udm-boot.service`, the upstream
    unit this repository carries, which runs every file in that
    directory in numeric order.
-   **`/data/custom`**, which is this program's: `bin/` for
    executables, `units/` for unit-file sources, `dpkg/` for the
    offline package cache, plus whatever directory a layer of the
    gateway asks for.

Three files are the mechanism itself.

**`udm-boot.service` is carried verbatim from upstream**, with the
commit it is pinned at in its own header, and it is delivered as a
*unit source* under `/data/custom/units` like any other. That makes a
wiped `/etc` recoverable by hand: the file the device needs is already
on the device, and copying it into place restores the chain that
restores everything else.

**`10-packages.sh` reinstalls what a firmware update wiped**, in one
transaction, because packages version-locked to one another cannot be
resolved a package at a time. *Which* packages is data — the union of
what the layers above require, rendered into the script — and
everything else about it is mechanism. When apt succeeds, the debs it
downloaded become the offline cache in `dpkg/`, which is the fallback
for the post-update boot where apt is unreachable. The cache is
refreshed by replacing the whole directory in one rename, so **`dpkg/`
is that script's alone**: a file this program wrote there would be
deleted by the next refresh and reported as drift forever after.

**`20-units.sh` converges the unit sources** into
`/etc/systemd/system`, enables them, and restarts the ones whose file
changed. It never restarts `udm-boot.service`, which is the unit
running it, and it mirrors no deletion, because the live directory also
holds units that are not this program's. Retirement happens from the
other side instead: deleting the resource that declared a unit removes
its source and, in the same session, disables the unit and removes the
live copy.

**The layers above reach the mechanism through it, not around it.** A
component that needs a script in the boot chain, an executable, a unit
or a directory asks the persistence layer for one and gets back a
resource of its own: where the file goes, what mode it takes and what
runs once it lands are the mechanism's decisions, while the file itself
belongs to the component that needs it and goes away when that
component stops declaring it. Directories are declared by a marker
under `/data/custom/.skeleton` whose hook creates the directory it
names — the push writes files, and the one directory that most needs
declaring is the one nothing may write in.

**New device automation is a systemd unit plus an executable in
`bin/`**, unless the operation manipulates systemd's own configuration
— installing its packages, its units, its nspawn files, its machines —
in which case it is a script in the boot chain. A unit gets its own
journal, its own failure in `systemctl status`, a restart policy, and
it can be run again outside boot; what it cannot get is a place before
the unit store exists, which is what the two scripts above need and
what the numeric order of `on_boot.d` expresses.

The container services of §1.1 do not go through this layer: their
files sit under a services root of their own and are converged by their
own recovery script.

## 2. ZeroTier network design

Architecture.md §5.3 decides *where* ZT terminates (the UDM) and *what
governs it* (ZT Central config in the `physical` stack via the bridged
`zerotier/zerotier` provider). This section is the network-level
design: roster, addressing, routes, flow rules, and cutover.

### 2.1 Member roster & addressing

The roster is **`conventions.overlay.ROSTER`**: one entry per member,
carrying the name Central shows, the `role` tag, the member's overlay
address, and — for a device that minted its own identity — its node id.
It lives in `conventions` rather than in one stack's data because two
stacks decide from it and neither owns it: `physical` declares the
membership from it and `dns` publishes the `*.zt` host block from it,
one A record per entry (declarative/dns.md §2). A member is therefore
admitted and named by the same declaration, and a device that leaves
the overlay leaves both together.

**An entry is the whole of admission**, which is what makes that tuple
a census rather than a list. A node id is minted by the device and
never changes, so it is an identity rather than a setting, and it is
recorded here beside the address; there is no configured mapping to
cross-check the roster against, and no way for the tag's permissive
default (§2.3) to reach anything undeclared. Two shapes carry that:
`EnrolledMember` for a device that arrived with an identity,
`GeneratedMember` for the two whose key material this program creates
in state — so a generated member with a node id written down is a
combination that does not type-check rather than one something has to
refuse. The roster's own invariants — names, node ids and addresses
unique, every address inside the overlay subnet, the gateway at
`10.144.1.1` when it is there at all, the roster no larger than
multicast reaches — are held by the test suite and by nothing at run
time: the roster is static code, so a run cannot break what a test did
not already catch.

**The gateway is absent until the ceremony records it.** Its node id
does not exist until the ZeroTier daemon — a container service this
stack delivers — has run once on the device, so §2.5 step 2 reads
that id off the device and adds the entry as a commit. Absence is the
whole of the mechanism: no member is declared for the gateway and no
`udm.zt` record is published for exactly as long as the entry is
missing.

**Every member is placed; none draws from the pool.** A pool address
would move, and the flow rules and DNS records naming it would not move
with it. The gateway's address and the two continuous-integration ones
are this repository's decision; the rest are the assignments Central
already made, recorded on the entry and re-declared as static from
there. A member whose display name contains a space keeps it: the
record helper lowercases and hyphenates the DNS label, rather than the
device being renamed in Central.

| Member | role tag | Notes |
| --- | --- | --- |
| `udm` | `infra` | The gateway: a static managed address, and the next hop of every managed route. Absent from the roster until §2.5 step 2 records the node id its daemon mints. |
| `Aetf-Arch-Homelab` | `infra` | The homelab host: a plain member, never a router (ZT carries no home-LAN routes), and the recovery side-door (§3). The one address the flow rules and the libvirt session look up rather than take from a constant. |
| `Aetf-Arch-VPS` | `infra` | The legacy deployment. Retires in Wave F together with its `10.42.0.0/24` route. |
| `haos` | `infra` | Home automation, reachable while the cluster is not. |
| `ci-physical` | `ci` | The `physical` identity domain: `plan-physical`, `up-physical`, and the drift matrix's `physical` entry. Identity generated in state (`zerotier_identity`), private key an Environment secret; `zt-physical` keeps it live in one job at a time (§2.6). IPv4-only (§2.3). |
| `ci-dns` | `ci` | The `dns` identity domain: `up-dns`, a pull request's `preview (dns)` and `prove (dns)`, and the drift matrix's `dns` entry — the LAN-touching work is the AdGuard rewrites (declarative/dns.md §3). Same generation and confinement, serialized by `zt-dns` (§2.6). IPv4-only (§2.3). |
| Personal devices | `personal` | Phones and laptops, each named in the roster. Full access — parity with sitting on the LAN. |

### 2.2 Managed routes

All LAN reachability via the UDM member's ZT address, one route per
subnet: 192.168.70.0/24 (the cluster VLAN), 192.168.80.0/24,
192.168.90.0/24, 10.0.5.0/24, and the `lan` pool 192.168.71.0/24 —
the pool is reached through the UDM's own BGP-learned route, one hop.
The legacy `10.42.0.0/24`-via-VPS route is deleted in Wave F. The
homelab host advertises nothing.

**There is no router object.** ZeroTier's model is an emulated switch:
a route is `{target, via}` on the *network*, and `via` is nothing but
an address belonging to some member. That member forwards because
forwarding is configured on the device, not because the controller
made it a router — ZeroTier has no concept of one. So the UDM is told
two separate things by two separate mechanisms: the route table here,
and its own routing and forwarding configuration as files on the box
(§1). The rest of the mechanism follows from that, and is worth
stating because the intuitive answers are wrong:

-   **Routes reach every member, not the ones that need them.** The
    controller hands the whole network configuration, routes included,
    to each member as it joins or refreshes. A route added here is
    added to every joined device.
-   **Each member installs them into its own operating system**, gated
    by a client-side setting: `allowManaged`, on by default for
    private ranges, and `allowGlobal` and `allowDefault`, both off. A
    phone off-site routes the home subnets into its ZT interface
    because its own client put them there, not because anything
    server-side steered it.
-   **A member sitting on one of those subnets does not refuse the
    route.** The client installs the overlapping route anyway and
    arranges to *lose*: it gives the route a high metric, so the
    kernel prefers the directly connected path. On BSD-derived
    systems the add simply fails against the existing route, to the
    same effect. Only the default route is ever overridden, and only
    under `allowDefault`, by splitting it into two halves that win on
    prefix length.
-   **The return path is not part of this at all.** A managed route
    fixes one direction. Replies come back because the UDM is the
    LAN's default gateway, which it is here; anywhere that were not
    true the return leg would need a static route on whatever is, or
    masquerading.

**Every managed route is IPv4, and the pool's ULA /64 is deliberately
not among them.** The overlay assigns no v6 address to any member
either (`assign_ipv6s` has all three schemes off), and the two facts
are one decision: the CI confinement rules in §2.3 end in a `drop`
pair, and a member holding a v6 assignment would have that pair eat
its own ICMPv6 neighbor discovery. Nothing on the overlay is reachable
over v6 that is not reachable over v4, so the single-family overlay
costs nothing and is what makes the confinement complete.

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
port literals come from `conventions`):

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

# CI confinement: four targets, each flow as outbound leg + return leg.
# Targets: UDM SSH (gw-config push), the UDM's UniFi Network API
# (443, the UniFi OS proxy — the unifi provider's controller calls,
# declarative/physical.md §4), the AdGuard APIs (alice/bob),
# the homelab host's libvirt SSH.
accept tseq role 2 and ipdest <udm-zt-ip>/32      and dport 22;
accept treq role 2 and ipsrc <udm-zt-ip>/32      and sport 22;
accept tseq role 2 and ipdest <udm-zt-ip>/32      and dport <unifi-api>;
accept treq role 2 and ipsrc <udm-zt-ip>/32      and sport <unifi-api>;
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
    today; the constraint is recorded, so roster growth cannot
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

1.  CI member reaches exactly its four targets (SSH banner / API
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
7.  **Join latency**: measure a fresh `ci`-tagged member's
    join→SSH-reachable time against the UDM (expectation and why it
    should beat the legacy 1–2 min: §2.6).

### 2.5 First bring-up

Steady state is circular: the gateway is reached over ZT, and the ZT
daemon on the gateway is a container service that same channel
delivers. The gateway's ZT identity is circular in the same way — a node
id is minted by the daemon's first run on a device, so it does not exist
to be authorized until the delivery has happened.

What breaks the cycle is the LAN, which reaches the UDM before and
independently of ZT, and an optional key of the `physical` stack:

**`gatewayBootstrapHost`** — a LAN address for the gateway, e.g. a name
the home resolvers already answer for. It answers one question: where
does the device answer today. While it is set, both providers that
reach the device dial that address instead of the roster's
`10.144.1.1` — the desired-state push over SSH and the controller's API
over HTTPS, one box behind two ports. Unset, which is the steady state,
both derive the address from `conventions`. Whether the gateway is a
member at all is a separate question with a separate answer: the roster
carries an entry for it or it does not (§2.1).

The ceremony, four steps and three applies:

1.  **Set `gatewayBootstrapHost`, then `physical` up.** The push goes
    over the LAN and delivers the services, the ZT container included.
    Nothing is declared for the gateway on the overlay. `workerGua`
    is unset here and optional for that reason: the address it
    carries is formed by the worker off this apply's own router
    advertisement, so the pinhole waits for step 3 (§4.2).
2.  **Read the minted node id off the device** — `zerotier-cli info` in
    that container — and add the gateway's entry to `ZT_ROSTER`, at
    `10.144.1.1` and with that id. It is a commit rather than a
    configuration change: a node id is an identity the device minted
    once and keeps.
3.  **Read the worker's GUA off the VLAN-7 advertisement** — the
    address it formed by SLAAC once step 1 declared the network and
    booted it — into `workerGua`, and **apply again, knob still
    set.** The roster now authorizes the member and assigns it
    `10.144.1.1`; the device joins the network it is the router of,
    and the inbound-v6 pinhole (§4.2) is declared for the first time.
    The managed routes are not added here: they are declared on the
    network resource, so step 1 already wrote them, each `via`
    `10.144.1.1`. What this step adds is a member at that address
    — until now the routes named a nexthop nobody answered for, which is
    all a route to an absent router ever is. Both halves of this step
    read a value the previous apply brought into being, which is why
    they are one step and not two.
4.  **Unset the knob and apply once more.** Every client is back on the
    overlay address, so this run dials over ZT — which is the
    verification rather than a formality: it rewrites the services through
    the path that is now load-bearing, and it converges only if that
    path carries the whole of it.

Two properties make the ceremony safe to repeat. The pinned host key is
a bare `ssh-ed25519 <blob>` line with no host name in front of it, so it
matches the device at either address and nothing is re-pinned when the
dial moves. And a moved dial address is an ordinary change rather than a
replacement (`providers/device_files/provider.py`): the file is rewritten at
the new address, and nothing is deleted at the old one — which, both addresses
being the same box, would delete what the same apply had just written.

**The ceremony is operator-local by construction.** Its first three
steps dial the LAN, and CI reaches the site over ZT and has no path to
the LAN at all — so a first bring-up cannot be a CI run whatever the
schedule says (migration.md Phase 0), and neither can any later recovery
that starts from a gateway which is off the overlay (§3).

Two things that are *not* part of the cycle:

-   **Managed routes are net-new additions** — the home-LAN and
    lan-pool routes via the UDM member appear where none existed;
    existing members gain reachability and lose nothing. No flip, no
    transition window.
-   **CI's per-run ZT join becomes load-bearing only after §2.4
    passes** — until the flow rules and routes are verified,
    `physical` runs stay operator-local.

There are no ordering edges between the three gateway domains: the
graph cannot express "authorize a member, wait for the device to join,
then dial it", because the middle step happens on the device and not in
this program. The ceremony is that ordering, performed by the operator
once.

### 2.6 CI join mechanics: two identities, serialized domains

Facts that shape it (decided 2026-08-24):

-   **A join cannot be reused across jobs** — each hosted-runner job
    is its own VM — and folding stacks into one job to share a join
    would collapse the per-stack Environment credential partition
    (ci.md §3). Per-job joins are the shape; their recurring cost is the
    latency of each join (below).
-   **One identity live in two places flaps** (ZT maps a node ID to
    one endpoint at a time), so concurrent jobs must never share an
    identity. Hence, one identity per domain — `physical` and `dns`,
    the two stacks whose jobs join — and each domain serialized by a
    **job-level `concurrency` group named after it**, `zt-physical` or
    `zt-dns`. The group has to name the identity rather than the
    workflow because previews, proofs, drift and the merge chain are
    different workflows joining with the same two identities, and a
    concurrency group is repository-wide. Mechanics and the accepted
    residual (a pending joining job can be superseded by a newer one):
    ci.md §2.
-   **Rejected: per-run generated identities** self-authorizing via
    the Central API — that puts the network-admin Central token into
    every environment that joins, dissolving the confinement the flow
    rules exist to provide.
-   **Join latency expectation**: kluster-code's measured 1–2 min
    wait-for-peer is dominated by NAT traversal toward a NATed host
    member (relay first, then a hole-punched direct path). Here the
    peer is the UDM itself, whose ZT socket (host-networking
    container) sits on the WAN interface un-NATed — the direct path
    should form on first contact, and traffic flows (slowly, via ZT
    relays) even before it does. Verified as §2.4 item 7;
    seconds-class expected, and if it stays minutes it is a per-job
    fixed cost, not a correctness problem.

## 3. Failure & recovery (playbook census)

Per the census discipline (state-backend.md §7): title, trigger, gist —
executable form ships with the implementation.

**Standing decision: an unreachable gateway fails the whole `physical`
preview.** Every gw-config resource diffs against the device rather than
against state, so a preview opens a session per resource; with the UDM
down, its ZT container down, or the overlay itself down, all of them
fail, and the run produces no plan. That is intended — a
preview that reports "no changes" about a device it never looked at is
worse than one that says it could not look — and the cost is real: while
the gateway is unreachable, no `physical` pull request can be previewed
and no `physical` change of any kind can land, gateway-related or not.
The side-door playbooks below are what shortens that window; a bring-up
knob is what covers a gateway that is off the overlay for a reason
(§2.5).

The `dns` stack is deliberately the other way round: an unreachable
resolver fails only its own rewrite resources and the rest of the zone
converges (framework/ci.md §2). The two are asymmetric because the
gateway is `physical`'s own management path, so a plan made without
reaching it would describe a device the apply cannot touch either,
while a resolver is a leaf whose absence says nothing about the records
at the registrar.

-   **ZT container down on the UDM** — trigger: physical-stack CI runs
    fail to reach the UDM; personal devices lose LAN reachability. The
    repair tool (gw-config push) itself rides ZT, hence the side-door:
    connect to the **homelab host's direct ZT address** (member-to-
    member traffic needs no managed routes), hop to the LAN, SSH the
    UDM, restart the unit or rerun the recovery script (§1.1). If the
    host is also down: physical presence (LAN).
-   **Firmware update wiped the services** — trigger: post-update, nspawn
    units gone. The recovery script re-materializes them autonomously
    (§1.1); verify ZT comes back last (it carries the management
    path). Fallback if host-networking nspawn misbehaves
    post-update: the unifios-utilities apt pattern (§5.3).
-   **UDM replaced** — trigger: hardware failure/RMA. Restore from the
    UniFi autobackup (the pull-direction yadm timer), re-run the
    gw-config provider for the services, re-authorize the *new* UDM
    member identity in the roster (identity lives in `/data`, lost with
    the box), re-point the managed routes at it — the §2.5 ceremony
    over again, bootstrap knob and all, since a replacement box is a
    device with no identity and no services (personal members' direct
    paths still work throughout).

## 4. Firewall target state

The as-designed panorama of the UDM firewall. The individual rules
were decided in their owning docs (architecture.md §3.4/§3.5, audit
M2, workloads.md §4); this section is the one place that holds the
complete set and the zone-matrix target state.

### 4.1 Ground truth (measured 2026-08-23)

-   The UBIOS zone firewall classifies forwarded traffic by
    **destination ipset**, not interface pairs.
-   All three pre-existing VLANs — br0 (server LAN `192.168.80.0/24`,
    the homelab host), br2 (IoT `192.168.90.0/24`, HAOS lives here),
    br5 (containers `10.0.5.0/24`) — sit in the **LAN zone**, and
    LAN→LAN is an unconditional predefined ACCEPT: **zero inter-VLAN
    isolation today**. This is a recorded dependency-in-force, not an
    endorsement (§4.3 is the plan to change it). The cluster VLAN
    (§1) does not join them: it is created in a zone of its own,
    which is the entire reason it is a separate network (§4.2).
-   The `lan` pool `192.168.71.0/24` (+ ULA /64) is deliberately
    never a network object (it would fight the BGP /32s,
    architecture.md §3.4), so it sits in no zone ipset: pool-bound
    traffic falls through to the **LAN→WAN chain** (ACCEPT today)
    and inherits any WAN-side machinery. Bootstrap verification: no
    NAT/IPS/content-filter interference on the LAN→pool path. Any
    rule naming the pool must use address groups — forever.
-   `zt*` interfaces match no zone; ZT-forwarded traffic rides
    FORWARD's default ACCEPT — the Central flow rules (§2.3) are its
    only policing layer.

### 4.2 Declared-objects census

The complete set — every controller-side object and rule the design
calls for, all declared in the `physical` stack via the bridged
filipowm/unifi provider (auth discipline: dedicated local admin + API
key, throttled retries — declarative/physical.md §4).

**Two address populations, two control planes.** The distinction runs
through every rule below, and the two are policed by different
machinery:

-   **The node subnet — VLAN 7 / `192.168.70.0/24` (§1) — *is* the
    cluster zone.** It is a network object, and it is placed in a
    **zone of its own** instead of joining the three networks in the
    LAN zone. That placement is what the VLAN is *for*: a node inside
    the LAN zone is a machine no policy can be written about, because
    LAN→LAN is a predefined unconditional ACCEPT (§4.1); a node in its
    own zone has an editable policy on every direction it talks in,
    and every later tightening is then a previewed diff rather than a
    re-architecture. What lives on it: Talos apid, the kubelet, the
    BGP session on 179, the worker's GUA. Routine paths deliberately
    do not cross the UDM into it — `talosctl` rides the NLB, home-side
    management rides ZT, host↔worker NFS rides the on-box `kvmbr1`
    leg, the peer-port forward is WAN-side — so the zone matrix
    governs the exceptional traffic, not the working traffic.
-   **The service VIP pool — `192.168.71.0/24` + ULA — is never a
    zone**, because it is deliberately never a network object (§4.1).
    Pool-bound traffic falls through the LAN→WAN chain, and the only
    thing that can police it is a destination address-group rule,
    which is exactly what rule 1 below is.

**"Open only what is served" is the pool's design already — but per
source population, not globally.** The IoT VLAN gets enumeration; the
trusted VLANs keep open access, because the pool VIPs are precisely
what the home is meant to reach — jellyfin *and* the admin UIs, from
personal machines. A global per-VIP enumeration would sever the
operator's own access and buys nothing rule 1 does not already buy
against the least-trusted population. Per-VIP enumeration for other
source VLANs stays available later as an adoption-triggered diff, the
same treatment as §4.3 phase 2.

Then the rules. Every zone pair that carries a policy also carries a
**`FirewallZonePolicyOrder`**: a policy whose position is whatever
creation happened to produce is a policy the design does not own, and
on a pair holding both a drop and an allow the position *is* the rule.

1.  **IoT → `lan` pool: default drop with one enumerated allow**
    (audit M2, amended 2026-08-24; ships with the cluster). The
    allow precedes the drop: **IoT → the media VIP, port 443** —
    the `media-gw` Gateway's dedicated `lan`-pool address
    (cluster-infra.md §2), which fronts the apps IoT devices
    legitimately consume (smart TVs and streamers → jellyfin, the
    first member). Then the drop: IoT → the rest of the pool
    (admin UIs — immich, qbittorrent, grafana — stay unreachable
    from the LAN's least-trusted population). Address groups are
    single-family, so both rules come in v4-CIDR and ULA pairs.
    **The firewall names only the stable media VIP** (a
    `conventions` literal): which apps are IoT-reachable is
    decided at the Gateway layer (`media-gw` route attachment, a
    review-visible per-app parameter) — app membership changes
    never touch a firewall rule. *Correction on record*: audit
    M2's "recorded cross-VLAN dependencies all originate
    cluster→IoT" was wrong — TV/streamer → jellyfin is an
    IoT-originated dependency the census missed; a blanket drop
    would have severed it at jellyfin's migration wave.
    Ordered allow-then-drop, and both ahead of the pair's predefined
    ACCEPT, or the drop is never reached.
2.  **cluster zone → External: allow**, both families, every
    protocol. A zone the controller has just been told about is
    denied against every other zone in both directions, so this is
    not a tightening but the policy that makes the zone usable: a
    node whose control plane is in a cloud region has to be able to
    leave the site.
3.  **cluster zone → Internal: allow**, both families, every
    protocol, return traffic admitted. This is the direction the
    recorded cross-VLAN dependencies run in — the home-automation API
    on the IoT VLAN and everything else a workload initiates toward
    the home. What a workload may call is decided where the workload
    is declared; narrowing it here would move that decision behind a
    gateway credential.
4.  **Internal → cluster zone: allow, with the IoT VLAN dropped ahead
    of it.** The allow preserves what the trusted VLANs had while the
    nodes shared the untagged LAN — debugging straight at a node, a
    ping, a host path that hairpins. The drop is a v4-CIDR/ULA pair
    naming the IoT VLAN as the source, ordered **before** the allow
    because here the allow is the broad rule: declared after it, the
    drop would match nothing while reading as if it were in force.
    Talos apid, the kubelet and BGP go off the table for the LAN's
    least-trusted population on day one, and nothing recorded is
    severed — the single IoT-originated dependency targets the pool,
    not the node subnet. Structurally it is rule 1 one zone over: the
    zone stays open, the untrusted subpopulation is carved out.
5.  **qbittorrent inbound-v6 pinhole**: to the worker VM's GUA on the
    cluster VLAN, plus the service port. Constraint on record: the
    zone-policy API matches
    literal IPs only (no prefix-relative objects), so the rule
    embeds the current GUA and is **re-declared when the dynamic
    home prefix rotates**; a stale rule degrades to the accepted
    outbound-only-v6 stage (inbound v4 unaffected) — annoying, not
    urgent.

    **The address is configuration, and `workerGua` is optional** —
    the one conditional rule of the census. The GUA is a SLAAC
    address the worker forms from what the VLAN declared here
    advertises, so it comes into being one boot *after* the apply
    that declares the VLAN, and no first apply can be given it. With
    the key unset this rule is not declared at all, which is the same
    outbound-only-v6 stage a stale rule leaves behind, reached from
    the other side and with nothing wrong pointed at. Step 3 of the
    bring-up ceremony (§2.5) is where it gets set. The v4 peer-port
    forward below is unconditional either way: it names an address the
    address plan states rather than one a booted machine reports.
6.  **qbittorrent v4 peer-port forward** — target the worker at
    `192.168.70.10`, and **the only port forward on the device**. No
    management inbound exists: cluster and Talos management ride the
    NLB, home-side management rides ZT.

Nothing else. A controller rule not on this census is drift.

### 4.3 Zone-matrix target state — two phases

**Phase 1 (ships with the cluster rollout)**: exactly the census
above. The cluster zone is **open to Internal and External, and open
from Internal with the IoT source carved out**. LAN→LAN stays open;
the zero-isolation fact remains a dependency-in-force, now with the
cluster VLAN sitting outside it as a zone whose policy can be
tightened on its own schedule.

The carve-out is the only reachability the cutover removes, and it
has **zero known dependents**: the sole recorded IoT-originated flow
is a television reaching jellyfin, which is a pool address and not a
node-subnet one. Anything discovered afterward is an enumerated-allow
diff on the pair, not a rollback.

**Phase 2 (deferred, adoption-triggered — the Longhorn treatment)**:
**IoT → {Internal, cluster zone, `lan` pool} as one default-drop with
enumerated allows.** Two of the three legs already exist — the
cluster-zone leg is census rule 4's drop, the pool leg is census rule
1 — so what adoption actually flips is **IoT→Internal**.

-   **Trigger**: migration complete and stable (Wave F done, legacy
    retired) *and* the IoT dependency census verified over an
    observation window — both, not either.
-   **Adoption-day facts to gather**: enumerate IoT-VLAN-originated
    flows into br0/br5 from a UDM traffic-flow observation window
    plus the recorded dependency list (media-consumption flows are
    already carved out structurally via `media-gw` — §4.2 — and
    carry over unchanged). Two dependents are known in advance and
    are what make this leg the hard one: **mDNS discovery from the
    televisions and media devices**, and **Internal personal devices
    → AirPlay-class sessions on those televisions** — a bidirectional
    pattern where discovery crosses the VLAN boundary both ways
    before the session runs Internal→IoT. Making that path reliable
    and then enumerating it is its own post-bring-up project,
    `kluster-ops#85`, deliberately not bring-up work; phase 2 cannot
    flip ahead of it without bricking casting. Special attention also
    to **HAOS — the IoT VLAN's most capable resident**: every
    integration's outbound target (LAN services, UDM APIs, cluster
    VIPs) becomes an allow-list candidate the moment the default
    flips.
-   **Shape when adopted**: an IoT→Internal default-drop zone policy
    plus enumerated address-group/port allows, joining the two legs
    already declared — previewed Pulumi changes over the
    already-existing declaration channel; adoption is a diff, not a
    project.
-   **Why deferred**: the dependency census is unverified, and a
    wrong drop bricks home automation mid-migration; nothing in the
    rollout depends on it — phase 1 already covers M2's actual
    exposure (IoT reaching the cluster's admin UIs and now its node
    ports too).
-   **Untouched by design**: the LAN→IoT direction stays open (home
    automation reaches its devices, and so do the AirPlay sessions);
    phase 2 constrains only what IoT may initiate.
