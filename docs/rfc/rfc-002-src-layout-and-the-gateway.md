# RFC 002: Source Layout, and the Gateway as a Component Tree

*   **Status:** Implemented, 2026-08-29. All eleven slices of §15 are merged,
    and the text below is kept as the accepted proposal rather than as a
    description of the system. What the system *is* lives in the design
    documents: the device's own mechanism and the units it runs in
    [physical/gateway.md](../physical/gateway.md) §1, the libvirt transport in
    [physical/homelab-host.md](../physical/homelab-host.md) §6, the
    dynamic-provider mechanism and its measured semantics in
    [framework/pulumi.md](../framework/pulumi.md) §5 with the rendered-configuration
    and version-pin mechanisms in §6 and §3.2, the provider, configuration and
    data rules in [style/pulumi.md](../style/pulumi.md), the source layering in
    AGENTS.md and `pyproject.toml`'s import contract, and everything declared —
    the component tree, the per-node capabilities, the roster, the stack's
    configuration surface — in [declarative/physical.md](../declarative/physical.md)
    and [physical/gateway.md](../physical/gateway.md). Where this text and a
    design document disagree, the design document is right: two decisions moved
    during construction, the device provider's endpoint and host-key pin staying
    declared resource inputs rather than travelling through `configure` (§7.4,
    §11), and the pin becoming a `conventions` constant a preview shows (§11).
*   **Created:** 2026-08-28
*   **Authority:** the style rules (`docs/style/`) are what this document
    obeys; where they are silent, a rule proposed here is marked **new rule**.
*   **Companion:** rfc-001 is the async-input framework and stays untouched.
*   **In scope:** the tree under `src/`; how the gateway device runs what it
    runs, and how that is declared; the overlay; talking to systems Pulumi has
    no provider for; providers and their credentials; rendered configuration;
    the `conventions` restructure; and the `physical` stack's configuration
    reading and API shape.
*   **Out of scope, each settled elsewhere:** the `dns` and `github` stacks'
    internal reorganization — they move into the new tree here and are
    otherwise untouched, and get a document of their own, though the provider
    rule of §8 and the mechanism choice of §7 apply to them too; the scripts'
    internal shape; and the shared test machinery.

--------------------------------------------------------------------------------

## 1. Context & Problem Statement

Everything the design names is implemented, and the implementation works. What
it lacks is a shape: five kinds of code share three directories, the gateway is
a set of module functions rather than the component the design calls it, another
program's configuration languages are Python string literals, providers are
constructed in some places and left ambient in others, and the data every stack
agrees on is a flat namespace in which values that are only correct together are
declared apart.

None of that is a defect a test can catch, which is why it is settled by a
document before it is settled by a diff. Two facts make now the moment:

1.  **Nothing here has been applied.** The `physical` stack has no state, so
    every rename in this document is free. After the first apply the same
    renames are replacements of live resources.
2.  **The apps layer has not started.** Whatever shape the gateway takes is the
    shape thirty application components will copy.

The document states the end state. The last section lists the order it is
reached in; nothing else here describes a transition.

--------------------------------------------------------------------------------

## 2. Source layout

### 2.1 Five kinds of code, five homes

| Kind | Home | What it is |
| --- | --- | --- |
| Stack programs | `kluster/stacks/` | Wiring. Reads the configuration no single component owns, builds the shared providers and the top-level components, exports outputs, declares no resource of its own. |
| Components | `kluster/components/<area>/` | Every reusable unit of resources, down to leaf resources. |
| Custom providers | `kluster/providers/<name>/` | The code that talks to a system Pulumi has no provider for (§7). |
| Scripts | `kluster/scripts/<name>/` | Console entry points declared in `pyproject.toml`. |
| Shared helpers | `kluster/lib/`, `putils` | Code with no resources and no area of its own. |

The two helper homes are not the same thing. `putils` is the Pulumi framework
of rfc-001 and knows nothing about this estate; `kluster/lib/` is estate-generic
but needs no Pulumi — configuration reading, template loading, workstation
slots, the Kubernetes helpers, the version pins.

`kluster/conventions/` sits below all of them: the decisions and identities
every layer reads and no layer owns.

### 2.2 The tree

```text
src/
  putils/                     # the framework (rfc-001), unchanged
  kluster/
    conventions/              # decisions and identities, one module per domain
    lib/                      # helpers with no resources
      config.py               #   typed reading of stack configuration
      templates.py            #   the one rendered-configuration mechanism
      workstation.py          #   the checkout-local slot mechanics
      k8s.py                  #   was kx.py
      versions.py             #   was config.py (image and chart pins)
    providers/
      device_files/           # was gateway/provider.py + gateway/ssh.py
      talos_factory/          # was the dynamic half of physical/image.py
      adguard_rewrites/       # was the dynamic half of dns/adguard.py
    components/
      gateway/                # was kluster/gateway/, minus the overlay
      overlay/                # was gateway/zerotier.py
      cloud/                  # was physical/{cloud,nodes,guardrails,storage}.py
      talos/                  # was physical/{image,talos}.py
      homelab/                # was physical/homelab.py
      backup/                 # was physical/backup.py
      dns/                    # was kluster/dns/ (contents unchanged here)
      apps/                   # empty; where application components land
    stacks/                   # physical, dns, k8s-base, apps, github
    scripts/                  # credentials, state_backend, update_crds
```

Every area is a package, single-component areas included, so that a reader can
guess a path from a name and an area that grows a second module does not change
shape to do it. `kluster/physical/` disappears: it named a *stack*, and the
things in it are components that the `physical` stack happens to declare today.

There are **three** custom providers, not two: the device files, the Talos image
factory, and the AdGuard rewrites the `dns` stack writes. All three move to
`providers/`; only the first two are this document's subject, and the third is
named here so that the layout has a home for it.

### 2.3 Import directions

The layering above is an import graph, and it becomes an import-linter contract
checked in CI beside the linter and the type checker. Layers, from the top:

```text
kluster.stacks  →  kluster.components  →  kluster.providers  →  kluster.lib  →  kluster.conventions  →  putils
```

A layer may import anything below it and nothing above it. On top of the layers,
four forbidden edges:

1.  **`kluster.scripts` imports neither `kluster.stacks` nor
    `kluster.components` nor `kluster.providers`.** A script is a program of its
    own; it shares `lib` and `conventions` with the declarations and nothing
    else. (**New rule.** The style rules name scripts as a kind of code without
    saying what they may reach.)
2.  **`kluster.providers` does not import `kluster.conventions`.** A provider is
    generic code for a class of system; the estate's decisions are its callers'.
    (**New rule.** The style rules say custom providers live apart from the
    declaration logic that uses them; this states what "apart" forbids.)
3.  **`putils` imports no `kluster` package at all.** Not a new rule so much as
    what `putils` is: the framework of rfc-001, which knows no estate.
4.  **Nothing imports `kluster.stacks`** except `kluster.main`, which dispatches
    them. (**New rule**, though it follows from the style rules' definition of
    a stack program as wiring: something another module imports for its contents
    is a component, whatever directory it sits in.)

Component areas may import each other — `homelab` names `talos`'s cluster type
in its signature — and that stays legal; a contract making the areas independent
would be enforcing a boundary the design does not have.

One import in the tree today breaks rule 1 in the other direction:
`physical/homelab.py` imports `scripts.credentials.workstation` to materialize
the libvirt transport into the checkout. The mechanics of a workstation slot —
find the checkout root, write `0600` into `0700` — are a helper, so they move to
`lib/workstation.py`; the credential-specific slot names stay in
`scripts/credentials/` and import them from there.

--------------------------------------------------------------------------------

## 3. Vocabulary

The naming rules produce a glossary, and the glossary is the contract the code
follows. It is kept in the `conventions` package's own module documentation,
where the style reviewer reads it.

### 3.1 Renames

| Today | Becomes | Why |
| --- | --- | --- |
| `Estate`, `estate.py`, "the estate" | `DeviceServices`, `services.py` | "Estate" means four unrelated things across the docs: this container set, the site's address plan, the DNS records no app owns, and the operator's own credential world. The style rules retire it by name. |
| `Seed`, `adguard_seed`, `seed_state` | `InitialState`, `initial_state` | "Seed" stays reserved for the two places it is the target system's own word: the `nocloud` seed image, and the credential seed kit. |
| `Dropin`, `Container.files` | `MountedFile`, `Container.mounted_files` | These are not systemd drop-ins. They are files written on the device and bind-mounted into the container read-only, and the name now says so — which frees "drop-in" for its real meaning. |
| `GwFile`, `GwArtifact`, `GW_*` | `DeviceFile`, `DeviceArtifact`, `gateway.*` | "gw-config" is the name of a convention the device supports, not a prefix every symbol needs. |
| `zerotier.Network` | `Overlay` | It collides with `unifi.Network`, which is a LAN. |
| `ZtMember`, `ZT_ROSTER` | `OverlayMember`, `overlay.ROSTER` | One term per concept, and the package path already says ZeroTier. |
| `Enrolled` | *(deleted)* | Its two fields become roster fields (§10.2). |
| `Firewall` (UniFi) | `SiteFirewall` | Distinguishes it from the overlay's flow rules, which are also a filtering policy. |
| `on_boot_script`, `20-kluster-estate.sh` | `recovery_script`, `20-kluster-services.sh` | Named for what it does rather than for the directory it sits in. |
| `parse_rootfs`, `parse_addresses`, `parse_members` | *(deleted)* | §10.2, §11. |
| `facts.py` | `lib/config.py` | §10.4. |
| `AUGMENTED_NODE`, "the augmented node" | `DEDICATED_VIP_NODE`, `NODE_VOLUMES` | One name for two unrelated capabilities that happen to sit on one machine today (§10.5). |
| `CacheVolume` | `NodeVolume` | It is a block volume attached to a node; what the volume is *for* belongs to whatever claims it. |

### 3.2 Which network a name means

Every class, method and variable that touches an address says which network the
address is on. Three adjectives, used everywhere and nowhere else:

*   **overlay** — the ZeroTier network. `overlay_address`, `OverlayMember`,
    `Overlay`. Never "ZT" in prose, never "network" unqualified.
*   **site** — the home LANs the gateway routes, collectively.
    `SiteFirewall`, `site_networks`.
*   **container VLAN** — the one site network the container services sit on,
    named as such because three of them hold an address there and the fourth
    deliberately does not.

One word this document borrows rather than coins: libvirt calls a virtual
machine a **domain**, and §13 uses it in that sense only. Nothing else here is a
domain — an area of `components/` is an area, and a DNS name is a name.

The machine at the middle of all this keeps three names, each for a role the
others do not fill: **the gateway** (what it does for the site, and the name of
the component), **the device** (what a provider writes to, and the thing that
has a userland, a host key and a firmware update), and **the UDM** (the
appliance, where the sentence is about hardware or the controller that ships
with it).

"Estate" survives in one meaning only — the DNS records that belong to no
application, which is what the `dns` package already calls them. Whether even
that one survives is the `dns` document's to decide; this one only stops adding
to the pile. It never again names the container services, the address plan, or
the operator's credentials.

--------------------------------------------------------------------------------

## 4. The device: how the gateway runs what it runs

This section is about the machine, not about Pulumi. It states the mechanism
that has to hold even if this program never runs again — which is the
point of it, because the device has to come back from a firmware update with
nothing but its own files. §5 is how that mechanism is declared.

### 4.1 What a service is, on the device

The device runs a routing daemon of its own and four containers under
`systemd-nspawn`. Everything either needs lives under `/data`, the one directory
a firmware update leaves alone: the root filesystem archives and the trees
unpacked from them, the unit files, the configuration each container reads, the
per-container writable state, and one recovery script.

**Three of the four containers sit on the container VLAN; the overlay daemon
runs in the host's own network namespace, and must.** `systemd-nspawn` shares
the host's namespace unless it is given a bridge, so that property is expressed
by *not* passing one — which makes it easy to lose by accident in a refactor,
and is why it is written here as a requirement rather than left as a fact of the
code. The daemon creates an interface when it joins, and the gateway routes
through that interface; an interface created inside a private namespace is
invisible to the router that has to use it, so the member would join and route
nothing. The declaration says the same thing by giving that service no address
(§5.3): no address means no bridge, and no bridge means the host's namespace.

The images are Alpine with s6-overlay rather than systemd, and two consequences
run through everything below. A container is told things through **its PID 1's
environment**, because that is what its own startup scripts read; a drop-in
written for a network manager the image does not run is a file nobody opens.
And a container is stopped with **`SIGKILL`**: s6 treats a gentler signal as
advisory, returns from it with its supervisors still running, and they hold the
unit's control group open until the next start fails on it.

### 4.2 What decides that a service must restart, and who does it

The recovery script is the whole mechanism, and it is deliberately the *only*
mechanism: it runs at boot, when nothing else is present, and it runs again
after every deployment as each file's post-apply hook. The recovery path is
therefore the path every apply exercises, so it cannot rot unnoticed.

What it guarantees, in order: every declared unit is installed and enabled; a
unit the declaration no longer names is stopped and removed; the routing
configuration is copied where the daemon reads it; a service that owns its own
configuration is given an initial state, but only where it has none; and a
service is restarted **only if a file that defines it changed**.

That last point is the load-bearing one, and it is why the health of a service
is decided by *files* rather than by a probe, through one named thing.

**The content stamp** is a file beside each service's state holding a checksum
over everything that defines that service: its unit, the digest marker of its
root filesystem tree, and every configuration file it mounts. The set of paths
the checksum covers is the service's **stamped set**; the stamp is the record of
what was last pushed, and comparing the two is how the script tells intended
content from pushed content without reading the device's mind. An unchanged
stamp plus an active unit means nothing to do. There is no health check on the
inside and no monitor: `Restart=always` in the unit is what handles a service
that dies, and systemd is the thing that notices. The script decides only
whether the *definition* moved.

The stamped set names the root filesystem's **digest marker** rather than the
tree itself, because walking a root filesystem to learn it has not changed costs
more than the restart it would save. The marker beside the *tree* rather than
beside the archive, because the archive's marker is written after the hook has
already run — a service that waited for it would learn about a new root
filesystem one deployment late.

Two files, two jobs, and they are not the same thing: a **digest marker** is
written by the push and records which published artifact a payload came from; a
**content stamp** is written by the recovery script and records what that script
last acted on. The first is an input to the second.

### 4.3 What the units express

Each service's unit states its own requirements, and the recovery script chooses
no start order:

*   every service wants and comes after `network-online.target`;
*   a service on the container VLAN binds to, and comes after, the bridge's
    device unit — so a container cannot be started against a bridge that does
    not exist yet. Today that race is absorbed by `Restart=always`, which is a
    retry loop standing in for a dependency.

The overlay daemon gets no such dependency, and the asymmetry is not an
oversight: **systemd creates device units only for kernel devices tagged
`systemd` in the `udev` database** — by default the block and network devices,
which is why a bridge has a unit to bind to and `/dev/net/tun` does not. What it
gets instead is a cheaper guard, for four reasons worth recording so that nobody
re-opens the question blind.

*   **There is no boot race to fix on this hardware.** The gateway's kernel
    builds TUN in rather than as a module, and its device nodes are materialized
    by `devtmpfs` before PID 1 exists. `/dev/net/tun` is therefore present
    before anything this design starts, and the recovery script runs late.
*   **A dependency without a `udev` rule would break the service outright.**
    Because nothing tags the node, `dev-net-tun.device` is loaded but inactive
    and cannot be started — systemd never activates a device unit itself,
    `udev` does. `Requires=` or `BindsTo=` against it is therefore unsatisfiable, and
    would turn a working service into a permanently failed one. The rule and the
    dependency are a matched pair that ship together or not at all.
*   **The rule would need its own recovery.** A firmware update preserves the
    persistent data directory and the systemd unit directory; `/etc/udev` is on
    no such list. The rule would have to be reinstalled from the same recovery
    script whose reliability it was meant to improve.
*   **What is actually missing is fail-fast, not ordering** — and that has a
    one-line answer. If the node were ever absent (a future firmware, a kernel
    change, a context where it is not passed through), the daemon does not
    crash: it logs that it cannot open the device and sleeps, so the unit stays
    *active* while the member is offline. `AssertPathExists=/dev/net/tun` on the
    unit converts that silence into a failed unit with a reason, costs no new
    mechanism, and needs nothing on the device. **That is what the units carry.**

The interface the daemon itself creates is not a candidate either: a unit cannot
wait on a device its own service brings into being. For scale, the established
recipe on these boxes runs the daemon under `podman run --device=/dev/net/tun
--net=host` from a numbered script in this same persistent boot directory, with
no unit at all — so no ordering, no assertion and no restart policy. Running it
under a unit is already more structure than the norm.

There are **no mutual dependencies between the four services**, and the units
say so by declaring none. The caddy instance proxies to the resolvers at request
time, not at start time; the overlay member carries the management session, not
the other containers' traffic.

### 4.4 The management session, and why one service restarts last

The deployment reaches the device over the overlay, and the overlay is carried
by one of the containers the deployment is updating. Restarting that container
severs the session that asked for the restart.

Three things together make that safe, and all three are part of the mechanism
rather than of the declaration:

1.  **Nothing restarts that did not change** (§4.2). Without that, every single
    apply would cut its own session.
2.  **The service carrying the session is restarted last**, so everything else
    has converged before the risk is taken. This is a property of the transport,
    not of the machine: expressing it as a unit ordering would state something
    false about how the device boots, so it lives in the script's restart loop,
    where the comment explains it.
3.  **An apply that dies there fails its own resource**, and the retry finds the
    work already done — because every step the script takes is idempotent and
    the content stamp for that service has not been written yet.

During a first bring-up there is no overlay yet: the container that will carry
it is the thing being delivered. The session runs over the device's LAN address
instead, and that is the whole of what the bring-up knob does (§10.2).

--------------------------------------------------------------------------------

## 5. The gateway as a component tree

### 5.1 The tree

```text
Gateway                       components/gateway/__init__.py
├── DeviceServices            components/gateway/services.py
│   ├── recovery script       one DeviceFile: what every other file's hook runs
│   ├── routing config        one DeviceFile: the daemon's own, applied by itself
│   └── Container × 4         components/gateway/container.py
└── SiteFirewall              components/gateway/unifi.py
```

The two named in title case under `Gateway` are components; the two in lower
case are single resources and stay resources — a component wrapping one file
would be ceremony, and the rule is that a *set* of resources with a name in the
design is a component.

`Gateway` is the device: the two doors that reach it are a shell and the
controller's API, and it owns both. It declares no resource of its own beyond
its children.

**The overlay is not under it** (§6). The gateway is a member of the overlay
with routes that bridge it to the site, which is a fact about the gateway; the
overlay's own configuration — who may join, what the rules are — is not the
gateway's business and does not go through it.

`declare_estate`, `declare_firewall` and `declare_zerotier` disappear: each is a
second name for a component and hides it from the reader (§12).

### 5.2 What `Container` takes

Each container service is a `Container` component owning everything that belongs
to it and nothing that belongs to a sibling:

*   its **root filesystem artifact** — a `DeviceArtifact`, pinned by digest,
    unpacked into the tree the unit boots;
*   its **unit file** — a `DeviceFile`;
*   its **mounted files** — one `DeviceFile` each, the configuration the image
    reads, bind-mounted read-only at the path the image expects;
*   its **initial-state file**, where it has one — the file the recovery script
    installs into the service's own working directory only when that directory
    holds none, because the software behind it rewrites the file afterward.

A `Container` also exposes the two facts its parent needs and nothing else: the
unit name, and its **stamped set** — the paths the content stamp covers (§4.2).
`DeviceServices` renders the recovery script from its children rather than from
a separate table, so a stamped set cannot name a file no resource declares.

### 5.3 How the containers are organized

**The services are declared as typed parameters, not as a mapping.** Two words,
kept apart for the rest of this document: the **census** is the table in
`conventions` — one `ContainerService` entry per service, holding the facts more
than one stack has to agree on (§10.1); a **declaration** is what
`DeviceServices` takes, one per census entry, naming that entry plus what only
the gateway knows about it, which is its image pin and its secrets. The two are
one-to-one, and "census" never means the constructor side:

```python
DeviceServices(
    name,
    caddy=CaddyService(service=conventions.gateway.CADDY, pin=..., acme_token=...),
    resolvers=tuple(ResolverService(service=entry, pin=...) for entry in conventions.gateway.RESOLVERS),
    overlay_daemon=OverlayDaemon(service=conventions.gateway.OVERLAY, pin=...),
    routing=RoutingSession(neighbour=..., password=...),
)
```

Each declaration **holds its census entry rather than naming it**. That is what
makes the binding a reference the type checker follows instead of a string
looked up at runtime, and it is why a resolver cannot be declared against a
service that has no address: `ResolverService` takes a `BridgedService`, and the
overlay daemon takes the host-networked one (§10.1).

A fifth member is a change to this signature, not a key in a mapping that a loop
may or may not look up. What each service *is* — where it keeps state, which
device nodes it needs, which environment its image reads — is a fact about its
image, so it lives in that service's own declaration type beside the component
that renders its unit. The image pins arrive as configuration, one key per
service, through the same mechanism that pins container images and charts
(§11.1).

The parameter is `overlay_daemon` rather than `overlay_member`, because
`OverlayMember` is a roster entry (§10.2) and this is the container that runs
the daemon. One term, one concept.

This retires `conventions.GW_ESTATE`, `estate.census`, and the three runtime
cross-checks that stood in for a type: a pin with no service, a service with no
pin, and a resolver bound to something that has no address all become
impossible to write. It also retires the function the one-idiom rule was raised
against — the census built one list three ways, and a signature builds none.

### 5.4 `SiteFirewall`

Unchanged in substance: the cluster VLAN as a network object, its own firewall
zone, the policy census, the port forward, the static host entries. Three
changes follow from elsewhere in this document:

*   the IoT VLAN's unique-local prefix stops being a literal declared beside the
    rules and is derived from the site network it belongs to (§10.1); it is the
    one spelling of the site prefix that lives outside `conventions` today;
*   the cluster subnet string is derived from the same structure rather than
    assembled from two constants at module scope;
*   the peer port is a convention rather than a configuration key (§11).

--------------------------------------------------------------------------------

## 6. The overlay and its rules

`Overlay` is a **top-level component**, built by the stack program beside
`Gateway` rather than under it. What it declares is the network, its managed
routes, the generated identities and the roster's members — the configuration of
a network that several machines are members of. The gateway is one of those
members, and the only one whose membership implies anything else: it is the
next hop of every managed route, which is a fact about the routes and is
declared here with them.

The gateway's *own* half of that arrangement — the routing daemon's
configuration, and the container that runs the overlay daemon — stays in
`DeviceServices`, because those are files on the device. The two components
therefore meet only in `conventions`: the roster says which address the gateway
answers at, and both read it.

### 6.1 What a managed route is, and what it is not

The overlay's routes are the reason this component exists at all, and the
mechanism behind them is easy to assume wrongly. Stated once, because the
component's shape follows from it:

*   **There is no router object.** ZeroTier's model is an emulated switch. A
    route is `{target, via}` on the *network*, and `via` is nothing but an
    address belonging to some member. That member forwards only because someone
    enabled forwarding on it; ZeroTier does not make it a router and has no
    concept of one. This is why the gateway's routing configuration lives in
    `DeviceServices` (§5) and only the route table lives here — they are two
    different systems being told two different things.
*   **Routes reach every member, not the ones that need them.** The controller
    hands the whole network configuration, routes included, to each member as it
    joins or refreshes. Adding a managed route adds it to every joined device.
*   **Each member installs them into its own operating system**, gated by a
    client-side setting — `allowManaged`, on by default for private ranges;
    `allowGlobal` and `allowDefault`, both off. So a remote laptop routes the
    home subnets into its overlay interface because its own client put them
    there, not because anything server-side steered it.
*   **A member that is physically on one of those subnets does not refuse the
    route.** This is the part worth writing down, because the intuitive answer
    is wrong: the client installs the overlapping route anyway and arranges to
    *lose*, giving it a high metric, so the kernel prefers the directly connected
    path. On BSD-derived systems the add simply fails against the existing
    route, to the same effect. Only the default route is ever overridden, and
    only under `allowDefault`, by splitting it into two halves that win on
    prefix length.
*   **The return path is not part of this at all.** A managed route fixes one
    direction. Replies come back only because the gateway is the LAN's default
    gateway — which it is here — or would otherwise need a static route on
    whatever is, or masquerading.

The durable home for this is the gateway design document's overlay section
rather than a framework document; it is stated here because the component
boundary above was drawn from it, and the design document is not this change's
to edit.

**`Overlay` declares no policy.** The flow-rule program arrives as a parameter,
composed by a pure function in `components/overlay/flow_rules.py` that takes the
facts and returns text:

```python
def flow_rules(
    *,
    gateway_overlay_address: IPv4Address,
    homelab_overlay_address: IPv4Address,
    resolver_site_addresses: Sequence[IPv4Address],
) -> str: ...
```

The stack program calls it and passes the result. The reason the composition is
not inside the component is that the confinement is not a fact about ZeroTier:
it is a fact about how continuous integration reaches this site. Its
destinations say so. One is a member of the overlay, at its overlay address,
opened on the one port a libvirt session needs — a fact about what a run does,
not about who is on the network. The other two are not members at all.

Those two are the interesting half, and the comment at the composition site says
why plainly rather than by implication:

> A run reaches the two resolvers at their **LAN** addresses on the container
> VLAN, not at overlay addresses, because they have none — they are containers
> on the device, not members of the overlay. The packets are routed by the
> gateway, and a routed packet still carries the destination it had before the
> forward, so the rule matches the LAN address.

`flow_rules.py` owns the whole program text, including the parts that belong to
ZeroTier itself: the tag declaration, the stock base filter, the final accept.
Splitting one program in one language across two modules to honor a layering
boundary would cost more than the boundary buys.

--------------------------------------------------------------------------------

## 7. Talking to a system Pulumi has no provider for

Two places in this stack drive something no Pulumi provider covers: the files on
the gateway device, and the Talos image factory's artifacts. Both are dynamic
providers today, and neither choice was written down. Pulumi names three
alternatives ahead of a dynamic provider, so the comparison is made here once
and referred to afterward.

### 7.1 The four options

*   **An existing provider.** Neither system has one, and the Terraform bridge
    this repository already uses for three SDKs has nothing to bridge: there is
    no Terraform provider for "desired-state files on a UniFi OS device" either.
*   **The Command provider** (`local.Command`, `remote.Command`,
    `remote.CopyToRemote`). Pulumi's guidance: use it when you need to *run a
    command as part of provisioning* rather than model a resource with a
    lifecycle. It does implement `diff` and `read` — its framework serves both —
    but only over its own inputs and state: `diff` compares the declared command
    and triggers, and `read` hands back the state it already has. **Neither ever
    looks at the target.** So a file edited on the device is invisible, and
    "make the device match this content" is not a thing the resource can mean.
    `Command.triggers` cause an update when an update command is given;
    `CopyToRemote.triggers` always replace.
*   **A dynamic provider.** Pulumi's criteria: no existing provider covers the
    resource, the logic is specific to a single program, and it need not be
    shared across languages or teams. Limitations, all of which apply here:
    Python and TypeScript only; `pulumi import` and `get` are unavailable;
    every resource stores a serialized copy of the provider in state, marked
    secret since 3.75.0; the package half of the type token is always
    `pulumi-python`, so a policy pack selecting on package cannot tell one
    dynamic resource kind from another — the `module`/`name` halves are the
    program's, and this repository already sets them
    (`pulumi-python:dynamic/gateway:File`); and — the one that shapes §7.4 — **a
    dynamic provider cannot be passed through `opts.provider` and does not
    inherit down a component tree.** Provider options are matched by the package
    name in the type token, and no provider resource can be `pulumi-python`.
*   **A full provider** (bridged or native). Gains everything the dynamic one
    lacks: `import`, `read`, cross-language use, ordinary provider inheritance,
    no per-resource blob. Costs a second language and a release pipeline in a
    Python repository, for something nothing outside this repository consumes.

### 7.2 The device files: a dynamic provider

**Recommendation: keep it.** Drift detection against the device decides it. What
makes this convergence rather than record-keeping is that `diff` opens a session
and compares the device's bytes with the declared ones, so a file someone edited
on the box appears in `pulumi preview` without a refresh. Of the four options
only a dynamic provider and a full one can express that, and the full one costs
a second language for a single device.

Pulumi's three criteria are met exactly: no provider covers this, the logic is
specific to this program, and nothing else will ever consume it. Of §7.1's
limitations only one costs anything here — a serialized provider on each of a
dozen resources, which is noise — and the missing `import` is moot, because a
device that already holds the files converges to the same content on the first
apply rather than needing adoption.

If a second device ever appears, or another repository wants this, a full
provider becomes worth its pipeline. That is the trigger to re-open this, written
down so the re-opening is a decision rather than a surprise.

### 7.3 The image factory: a dynamic provider, more narrowly

This one is a closer call, because the resource does no remote management at
all: it fetches a URL, decompresses it to a path on the machine running the
program, and deletes that file on destroy. It deliberately reports no drift — a
continuous-integration runner starts with none of these files, and a refresh
that called a missing file a deleted resource would take the worker's disk with
it.

`local.Command` would express that: a create command, a delete command, and the
URL as a trigger. **Recommendation: keep the dynamic provider**, on three
grounds, none of them large:

*   the download is checked for truncation against the length the server
    declared, which a shell pipeline would have to reimplement;
*   it needs no `curl` and no `xz` on the machine running the program, and the
    runner is not otherwise required to have either;
*   the seam a test replaces is a Python function, not a shell command.

The honest summary is that the Command provider would also work here, and the
reason to say so is that the *next* such need should be weighed rather than
assumed into a dynamic provider by precedent.

### 7.4 Connection state: a stateless provider

Pulumi documents dynamic-provider serialization for JavaScript only, so the
Python semantics this design rests on were established by experiment against the
installed versions (Pulumi 3.257.0, `dill` 0.4.1) in a scratch project with a
file backend. The measurements are recorded in §7.5; each claim below cites the
one that supports it.

**A native provider has a resource; a dynamic one does not**, and the design
follows from that. When a program builds a `kubernetes.Provider`, that provider
*is* a resource: it appears in
state with its configuration as properties, and changing what it points at is a
diff on a named object. A dynamic provider has no such object —
`pulumi.dynamic.ResourceProvider` is a plain class, not a `ProviderResource`,
and there is nothing for `opts.provider` to point at (§7.1). Instead, the
provider *instance* is pickled into a reserved, secret property on **each**
resource it manages, `__provider`.

**The provider carries no connection state.** Its attributes are unset in the
program, `__getstate__` returns nothing, and the values it needs are read in
`configure`, which runs inside the resource-provider process and receives the
stack's configuration — including secrets, already decrypted (E2). What lands in
state is then 55 bytes naming a module and a class, with an empty state
dictionary: inert, identical for every resource, and unchanged by a credential
rotation (E3).

That is the whole of the connection story: **the credential lives in stack
configuration and nowhere else.** It is not on a resource, not in a pickle, and
not passed between components. Rotating it is an edit to configuration.

**The provider makes its own consequences visible, in `check`.** With an inert
pickle nothing else would be, and the mechanism for it is the one hook that runs
before every diff: `check` receives the resource's inputs and returns the inputs
the engine will store and compare, so a provider may **add** properties there.
Two are added, and neither is declared by any caller (E8):

*   **`session`** — the endpoint, plus a short digest of the credential, both
    taken from the values `configure` put on the provider. A rotation or an
    address move changes it and the preview says exactly what happened:
    `~ session: "host-1#9d6fb67570c1" => "host-1#bab3d6bf12a7"` (E10).
*   **`provider_version`** — a constant in the provider module, bumped by hand
    when its behavior changes. This is not ceremony: a provider class imported
    from a module is pickled **by reference**, so editing the body of `create`
    changes not one byte of state and produces no diff at all, leaving the old
    outputs in place (E1). Injected the same way, bumping it renders as
    `~ provider_version: "1" => "2"` (E10).

**The program therefore never touches the credential at all.** It declares the
path, the content and the mode; the session, its fingerprint and the version are
the provider's business, computed in the provider's process from the
configuration only the provider reads. That is the whole gain over declaring
them program-side: one less place the secret is handled, one less derivation to
keep in step, and a resource whose declaration says only what the *caller*
meant.

Three facts about the mechanism the implementation has to respect, each of them
a way to get this wrong:

*   **`diff`'s two bags are not symmetrical.** `olds` is the stored **output**
    bag — it carries whatever `create` or `update` returned — while `news` is
    the **checked input** bag. A provider that compares every key sees each
    create-time output as a difference and reports a change on every single run;
    the comparison is over the checked-input keys (E7, where exactly that
    mistake produced a spurious update).
*   **`check` does not run on refresh.** A refresh calls `configure`, `read` and
    `diff` only, so the values compared there are the ones already in state
    (E10).
*   **The injected value lands in state in the clear.** A property the provider
    synthesizes carries no secret marking, however secret the configuration it
    came from — so the digest is plaintext in state and in previews. That is the
    intended outcome, and it is the same declassification as before rather than
    a new one: a truncated digest of a credential is not the credential, and a
    redacted value would make the diff illegible. What changes is where the
    decision sits — inside the provider, where it is a line of code and a
    comment, rather than in the program as an `unsecret` call (E10).

**A requirement on the implementation, not a consequence of it.** The two
injected properties change without the device changing, so `update` must
distinguish them from the rest. When the only difference is the session
fingerprint or the version — path, content, mode and ownership all equal — the
update **re-stamps the resource and touches the device not at all**: no rewrite
of bytes the device already has, and never a delete and a create. The diff stays
visible, because that is its whole purpose; the actuation behind it is a no-op.
Getting this wrong would rewrite every file on the gateway on every credential
rotation, which is the opposite of what the mechanism is for.

**What this costs.** With `__getstate__` empty, the provider's attributes do not
exist until `configure` has run — safe because the plugin calls `configure`
immediately after deserializing and before any operation (E2), and worth a
comment at the top of the class so nobody "fixes" it by adding a default.

**Alternatives measured and not taken.** Deriving the fingerprint in the program
works, at the cost of reading the credential there and declassifying it with an
explicit `unsecret` — without which the diff says only that something opaque
changed (E3). Returning it from `create` as an output also works as a record
(E4, E9), but an output cannot carry a rotation into a preview, because the
comparison the engine renders is against the checked inputs. `check` is what puts
the value on the side of that comparison.

This is the second reading of §8's rule, and §8.1 states both: a provider-only
credential is read where the provider is configured, which for an ordinary
provider is the line that builds it and for a dynamic one is `configure`.

The resource identifier becomes the path on the device alone, a provider
instance now standing for exactly one device. It is minted at create, never
re-derived afterward, and never a replace trigger: only the path is a
replacement, as today. That is what keeps the first-bring-up knob safe — moving
the session from the device's LAN address to its overlay one (gateway.md §2.5)
stays an update to the same resources, and never a delete and a create.

The same shape applies to the third dynamic provider in the repository, the one
that writes AdGuard rewrites: its endpoint, username and password are inputs on
every rewrite today. Fixing it belongs to the `dns` document; the rule is stated
here because it is one rule.

### 7.5 The measurements

Run against Pulumi 3.257.0 with `dill` 0.4.1, in a throwaway project on a file
backend. The durable home for these is `docs/framework/pulumi.md`; they are
carried here until that page is written.

| | Question | Result |
| --- | --- | --- |
| **E1** | What lands in `__provider`? | A class imported from a module is pickled **by reference**: 42 bytes naming the module and the class. Instance attributes *are* serialized, in the clear inside the secret property. Class attributes are not. A class defined in the entrypoint module is pickled **by value** — 856 bytes carrying its code objects and source path — so where the class lives decides which rule applies. |
| **E1** | What changes it? | Editing a method body of a module-level provider: **no change, no diff, no update**, and stale outputs stay. Changing an instance attribute: an update, rendered as `~ __provider: [secret] => [secret]`. Moving the class to another module changes it, the module name being part of the pickle. |
| **E2** | Is `configure` real? | Yes. Called in the provider process, once per process, before the first operation. `req.config` keys carry the project as their namespace, and **secrets arrive decrypted** — the plugin unwraps them and tells the engine it does not accept secret values. |
| **E3** | Does a stateless provider work? | Yes. With attributes unset in the program and `__getstate__` returning `{}`, every operation ran correctly after deserialization, and `__provider` was 47–55 bytes and constant across a rotation. |
| **E4** | Do provider outputs become properties? | Yes — values returned by `create` beyond the declared inputs appear as resource properties. They cannot by themselves carry a change into a preview, which compares against the checked inputs. |
| **E6** | Can an operation reach another resource's state? | No. Each method receives the property bag of the resource being provisioned; there is no engine handle and no lookup call. |
| **E7** | What do `check` and `diff` receive? | `check` gets the stored **input** bag as `olds` and the program's raw inputs as `news`. `diff` gets the stored **output** bag as `olds` and the **checked** input bag as `news`. The asymmetry is a trap: comparing every key reports a change on every run, because create-time outputs are in `olds` and never in `news`. |
| **E8** | Can `check` add properties? | Yes. Properties added to the returned inputs are stored as inputs, reach `create`, and take part in the engine's comparison. `check` runs once per process before the first operation, in both preview and update. |
| **E9** | Does `update` return properties? | Yes — its outs replace the stored output bag, so a record of which session last wrote the resource stays current. |
| **E10** | Does the injected design work end to end? | Yes. A rotation with **no program-side involvement** renders `~ session: "host-1#9d6fb67570c1" => "host-1#bab3d6bf12a7"`; a version bump renders `~ provider_version: "1" => "2"`; an unchanged run reports `unchanged`; a refresh calls `configure`, `read` and `diff` but **not** `check`. The injected value is stored in plaintext — a provider-synthesized property carries no secret marking. |

`refresh` and `destroy` need no special flag on this version: both ran plainly,
and both called `configure` before `read` and `delete`.

### 7.6 Where the providers live

`providers/device_files/` holds the two resources, their providers, the
exceptions they raise and the SSH transport. `providers/talos_factory/` holds
the image-factory resource that is today mixed into `physical/image.py`; the
`TalosImage` and `TalosNocloudImage` components that use it stay components, in
`components/talos/`. Neither package imports `conventions` (§2.3), which is
already true of both today and is what makes them providers rather than
declarations.

--------------------------------------------------------------------------------

## 8. Providers, explicitly

### 8.1 The rule

**Every provider this program uses is explicit, and the credential that
configures it is read at the line that builds it — wherever that line is.** That
is the whole rule, and it is the only part that is not negotiable: a provider
and the secret that opens it are one thing, and separating them means a reader
has to hold two files in their head to answer "what does this authenticate as".

*Where* that line sits follows from which component the connection belongs to:

*   **A provider that is one component's implementation detail is built inside
    that component**, which reads its own credential from stack configuration
    there. The controller API and the login on the device belong to `Gateway`;
    the overlay's administration token belongs to `Overlay`; the libvirt session
    belongs to `HomelabHost`; the backup account belongs to `BackupBucket`,
    which is the only thing that touches it.
*   **A provider several components share is built by the stack program** and
    set on each of them. The cloud provider is the case: the network, the nodes,
    the guardrails, the block volumes and the image import are five components
    against one account, and a provider built inside any one of them would be
    reached into by the rest.

Reading configuration inside a component is a deliberate use of a mechanism
built for it. Stack configuration is a global, and globals are to be used
carefully — but encapsulating a component's implementation detail is exactly
what this one is for, and the alternative is threading a secret through a
constructor that has no other opinion about it. The trade is the familiar one
between a global and a parameter, and it is made here in favor of the global for
this one category of value: **credentials that configure a provider and are read
by nothing else.** Everything else a component needs still arrives as a
parameter.

**New rule**, extending the style rules rather than contradicting them: they
already place a provider with its owner, and what is added is that the
credential is read there too, and that a provider with several consumers is the
stack program's.

Four consequences:

*   **Parent-ship is unaffected.** Which component is whose parent is a
    statement about the architecture, and it does not move because a provider
    does. `SiteFirewall` stays a child of `Gateway` whoever built the controller
    provider; the overlay stays a top-level component beside it (§6).
*   **`child_opts(provider=...)` disappears from component bodies.** A provider
    set on a component — whether it was built inside it or handed to it —
    becomes the default for its whole subtree, transitively, because each child
    inherits its parent's provider map and the first match by package name wins.
    So the controller provider reaches `SiteFirewall`'s zone, network, policies
    and port forward without any of them naming it. An invoke needs
    `InvokeOptions(parent=...)` to inherit — given a parent it takes that
    parent's provider, and given neither it takes the default one.
*   **The ambient namespaces retire.** The cloud and backup providers are
    configured by ambient namespaces today — configuration acting at a distance,
    where the same program run in a different environment declares against a
    different account. `pulumi:disable-default-providers`, listing the packages
    this program uses, turns "somebody forgot the provider" from a silent
    fallback into an error. It lists them rather than saying `*` because
    dynamic resources *depend* on the `pulumi-python` default provider: `*`
    would disable the one default provider this program still needs.
*   **A credential that configures a provider reaches no component's
    signature.** Not the component that builds the provider — it reads it — and
    not any component below. For a **dynamic** provider the same invariant is
    satisfied one step further in: there is no construction line to read it at,
    so the provider reads it itself from stack configuration inside `configure`,
    in its own process (§7.4). Either way the credential and its provider stay
    together, and nothing between them carries it.

### 8.2 Forgetting the parent, and why a transformation cannot fix it

Everything in §8.1 rests on a child being a child: a resource inherits its
provider from its parent, so a resource constructed inside a component without
`parent=` set inherits from the stack instead, silently gets the default
provider, and — with default providers disabled — fails with an error about a
missing provider rather than about a missing parent. It is the easiest mistake
in the codebase to make and the least obvious to read.

**The mechanism that looks like the answer is closed.** Pulumi's Python SDK runs
registered transformations at every resource registration and lets them rewrite
properties and options, so a stack transformation could in principle read a
context variable naming the component under construction and fill in the parent.
It cannot: the SDK compares the returned options and raises
`Transformations cannot currently be used to change the 'parent' of a resource.`
The parent is what selects which transformations run and what other options are
inherited, so it is fixed before transformations see it.

**So the framework enforces instead of repairing**, in two parts, both in
`putils`:

*   **A context variable naming the component being constructed.**
    `Component.__init__` pushes itself onto it and `register_outputs` pops it —
    a pairing this repository already treats as mandatory, which is what makes
    it a usable scope.
*   **One stack transformation that refuses.** It cannot set the parent, but it
    can see that a resource is being registered while a component is under
    construction and its options name no parent, and fail with the resource's
    name and the component's. The mistake becomes impossible to commit rather
    than merely discouraged, and it is caught at construction rather than at
    apply.

`child_opts()` stays the ergonomic path, and now it has a backstop. This is a
change to the framework rather than to any stack, so it lands as its own slice
(§15) and carries its own test: a resource constructed without a parent inside a
component raises, and the same construction outside one does not.

One thing this is not: the invoke rule in §8.1 concerns *provider* inheritance
for a function call, not the parentage of resources. An invoke takes a parent
only in order to find that parent's provider.

### 8.3 Every provider this stack uses

| Provider | Built by | Reaches | Credential, read at that line |
| --- | --- | --- | --- |
| cloud | the stack program | set on the cloud, Talos-image and node-volume components | the account's user, fingerprint and private key |
| controller | `Gateway` | set on its `SiteFirewall` child | the controller API key |
| device files | `Gateway` | its own children, as an object (§7.4) | none at construction — the provider reads it in `configure` (§7.4) |
| overlay | `Overlay` | its own children, by inheritance | the network administration token |
| libvirt | `HomelabHost` | its own children, by inheritance | the host's SSH private key (§8.4) |
| backup | `BackupBucket` | its own children, by inheritance | the backup account's key pair |

Only the first is shared, and that is what puts it in the stack program: five
components declare against that one account. Its region and tenancy come from
`conventions` (§10.3) rather than from configuration, so the line that builds it
reads exactly the three values that are secret. Everything below it takes
neither the provider nor the credential.

The backup row is a judgment worth stating: the backup account has exactly one
consumer today, so its provider is built inside that consumer. If a second
component ever declares against B2 — a second bucket, a restore drill's own
credential — it moves up to the stack program by the same test that put the
cloud provider there.

The device-files row is the dynamic one, and it differs twice. Its provider
instance carries nothing, so `Gateway` constructs an empty one and hands it down
the tree as an object — the mechanism cannot carry a dynamic provider through
resource options (§7.1). And its credential is read neither by the component nor
by the stack program but by the provider itself, in its own process (§7.4).

### 8.4 The libvirt transport's absolute paths

The libvirt provider is configured by a URI, and this program builds that URI
from two files it writes into the checkout: the client identity and a one-line
`known_hosts` carrying the pinned key. Both go in as **absolute** paths, so the
provider's configuration — which is a resource input, kept in state — contains
the path a particular machine happened to have. Run the same program from a
checkout at another path and the URI differs, so the provider diffs.

It is noise rather than danger: the bridged provider does not mark `uri` as
forcing a provider replacement, so the diff is a provider update and nothing
below it is replaced. But it is a diff that can never be resolved, on a stack
where a clean preview is the merge gate.

**The transport is the provider's own, not libvirt's**, and that decides the
options. The bridged version parses the URI itself and dials with Go's SSH
client; `qemu+libssh://` and `qemu+libssh2://` are *not implemented* and return
an error naming the transport, and there is no C libvirt in the provider to fall
through to. So the choice is within `qemu+ssh`:

| Option | Removes the host-specific value? | Cost |
| --- | --- | --- |
| **Relative paths** | Yes, entirely | Near zero |
| `${VAR}`-templated absolute paths | Yes | A new out-of-band contract: every invocation path must export the variable, and an unset one expands to nothing rather than failing |
| `sshauth=agent`, no `keyfile` | Only the identity's path | Offers every key in the agent to a root-equivalent endpoint, and the pin's path remains |
| `libssh` / `libssh2` | — | Not implemented |

**Recommendation: relative paths, resolved against the checkout root.** The
provider expands environment variables in both values — and, for the identity
only, a leading `~/` — then opens them without anchoring them anywhere, so a
relative value resolves against the plugin process's working directory. Pulumi
sets that to the project's `main` directory when the project declares one, and
otherwise to the directory holding `Pulumi.yaml`. This project declares no
`main`, so the two coincide and both are the checkout root — but the caveat
belongs in the comment beside the code, because adding a `main` later would move
the anchor silently. Pulumi's own documentation prescribes exactly this for
exactly this symptom: a path in a resource property should be relative to the
working directory, or running the project on two machines produces diffs.

The hazard, if the URI is wrong rather than merely non-portable, is worse than a
diff: a provider that cannot reach the host reads every domain as absent and
clears its id — which for the worker VM means the next apply proposes creating
a machine that is already there.

**Which directory, and what that directory means.** The two files land in
`.credentials/libvirt/`, where they are today, and the relative value in the URI
is therefore `.credentials/libvirt/identity`. That is a decision worth writing
down rather than inheriting, because it puts two different kinds of thing in one
directory:

*   a **slot** is durable and written by the credentials command — a kit, a
    passphrase, a client bundle — put there once and read by later runs;
*   a **working file** is derived, written by the stack program from stack
    configuration on every run, owned by the program and disposable.

The libvirt identity and its `known_hosts` are the second kind. They stay in
`.credentials/` anyway because that directory exists for one reason — it is the
`0700` boundary, the single answer to "what on this machine is secret" — and a
second git-ignored directory would mean a second boundary to establish and get
right. The distinction is recorded in the code beside both writers, so a reader
does not conclude that everything under `.credentials/` is a slot, and so nobody
later "cleans up" a working file expecting a command to have put it there.

Three things stay as they are, and each has a reason:

*   **The `$` guard stays.** Environment expansion runs on relative values too,
    so a checkout under a path containing a `$` still has to be refused rather
    than corrupted.
*   **The agent stays off.** `sshauth=privkey` is not incidental: the provider's
    default is `agent,privkey`, and an agent would offer whatever the operator
    happened to have loaded, making a runner and a workstation differ — which is
    the rule the gateway's own session follows too.
*   **The pin stays a file.** The transport has no parameter for an inline host
    key; a one-line `known_hosts` written beside the identity is the only
    mechanism it offers.

What must be proven live rather than from source: that the plugin's working
directory is the project root on the continuous-integration runner as well as on
a workstation. Everything else above is settled from the provider's code.

The contrast with the gateway's own SSH session is worth stating, because the
two look alike and only one has this problem: the gateway's private key is never
written to disk. It goes from configuration into the SSH library as bytes, so
there is no path to put in a resource input and nothing machine-specific to
diff. Where a mechanism allows it, that is the better shape.

--------------------------------------------------------------------------------

## 9. Rendered configuration comes from files

### 9.1 The mechanism

One mechanism for the repository, in `lib/templates.py`, and it works on
**directories** rather than on single files, because that is what the callers
after this one need: an application's configuration is a directory that becomes
a config map or the plaintext half of a sealed secret, and the legacy repository
grew exactly that helper (`renderStaticFiles`, whose keys are relative paths
under a stripped prefix). This is the lower layer such a helper is built on.

```python
def render_tree(package: str, directory: str, params: object | None = None) -> Mapping[str, str]: ...
def render(package: str, name: str, params: object | None = None) -> str: ...
```

`render_tree` walks one directory inside a package and returns
`{relative path: contents}`. `render` is the single-file case, for the callers
that want one string.

**The `.j2` suffix decides, and the suffix is stripped from the key.** A file
named `Caddyfile.j2` is rendered with the parameters and lands under the key
`Caddyfile`; a file named `dashboard.json` is copied through byte for byte and
lands under `dashboard.json`. That is the one ergonomic difference from the
legacy helper, which decides per *call* — a directory holding both kinds needs
two calls and two globs there, and here it needs neither. It also means a file
that must keep literal `{{ … }}` — a Grafana dashboard, a Go template another
controller will render later — is safe by construction rather than by the caller
remembering not to pass parameters.

Parameters are a frozen `dataclass`, which is what makes a template's inputs
typed at the call site rather than a bag of names. Files are located through
`importlib.resources`, so a template is found relative to its package and works
from a checkout and from an installed wheel alike.

Jinja2 rather than `str.format` or `string.Template`, for three reasons: it is
already a dependency of this repository; the files that need this have loops and
conditionals — a unit's argument list, the recovery script's case arms, the flow
rules' repeated destinations — and the alternatives would push those back into
Python, which is the thing being fixed; and `StrictUndefined` makes a parameter
the caller forgot an error at render time rather than an empty line in a
configuration file. The environment is fixed at `StrictUndefined`,
`keep_trailing_newline=True`, and automatic escaping off, since nothing rendered
here is HTML.

Templates live in a `templates/` directory inside the component's package, so
that the component and its rendered files move together.

### 9.2 What moves

| Literal today | Becomes |
| --- | --- |
| `estate.frr_config` | `gateway/templates/frr.conf.j2` |
| `estate.unit_file` | `gateway/templates/container.service.j2` |
| `estate.on_boot_script` | `gateway/templates/recover-services.sh.j2` |
| `estate.caddyfile` | `gateway/templates/Caddyfile.j2` |
| `estate.adguard_seed` | `gateway/templates/adguard-home.initial.yaml.j2` |
| `zerotier.flow_rules` | `overlay/templates/flow-rules.zt.j2` |
| `homelab.disk_tuning_xslt` | `homelab/templates/disk-tuning.xslt` (verbatim) |
| `image._schematic_document` | `talos/templates/schematic.yaml.j2` |

The Python functions keep their names and signatures — in the renamed modules
of §3.1, so `estate.unit_file` is `services.unit_file` — and become one line
each, so the shell quoting, the ordering guarantees and the rendered text all
stay under test exactly as they are. The one verbatim file in the table is what the
suffix rule is for: it carries XSLT braces and needs no parameters.

### 9.3 The gateway's own certificate

The caddy configuration is the one template whose content changes, and the
reason is a decision recorded on 2026-08-28. The gateway's three vhosts are
names in the primary zone that resolve nowhere publicly (dns.md §4), and the
gateway issues their certificate itself so that its TLS keeps renewing while the
cluster is down. Two consequences the template has to carry:

*   **One wildcard certificate, not three per-name ones.** Every issued
    certificate is published in Certificate Transparency logs, and per-name
    issuance would republish exactly the census that resolving nowhere was meant
    to hide.
*   **The wildcard alone, and never the apex.** Let's Encrypt counts its
    duplicate-certificate limit by identifier set across accounts, so two
    issuers asking for the same set share one weekly window and a crash-looping
    renewal on either side can lock the other out. The two sets here already
    differ, and the way to keep them differing is for the gateway to ask for
    less: the apex is a name the cluster issuer serves publicly, and its
    certificate carries the apex and the wildcard together. The gateway serves
    none of those public names — it has no business with the apex — so its
    request is `*.<zone>` and nothing else.

So the file becomes one site block for `*.<zone>`, with the three vhosts matched
inside it and everything else refused. Two things are checked against the live
systems in the slice that lands this, rather than assumed here: the exact
directive spelling for the caddy build the device runs, and the identifier set
on the cluster issuer's current certificate — the argument above rests on that
set, and a design that quietly stopped including the apex would put both issuers
back in one bucket.

--------------------------------------------------------------------------------

## 10. The `conventions` restructure

### 10.1 Shape

`conventions.py` becomes `kluster/conventions/`, one module per domain, and its
constants become structures — the illegal-states rule applied to data: values
that are only correct together are declared together, so using one without its
siblings does not parse.

| Module | Holds |
| --- | --- |
| `identity.py` | The cluster name, the stack and appliance names, the label domain. |
| `site.py` | One `SiteNetwork` per home network, one `AddressPool` for the `lan` pool, the site's unique-local prefix. |
| `overlay.py` | The overlay subnet, the roles, the managed routes, `ROSTER`, the network's own id. |
| `gateway.py` | The device's paths, account and pinned host key, the service census, the vhosts, the resolver API port. |
| `homelab.py` | The worker node's name, address and sizing, the host bridge, the host's pinned key. |
| `cloud.py` | The node fleet, node sizing, the VCN plan, the per-node capabilities of §10.5. |
| `cluster.py` | Pod and service ranges, BGP, the load-balancer pools, the Gateway API names, storage classes. |
| `backup.py` | Retention classes, bucket names, repository layouts. |
| `dns.py` | Zones, mirrors, anchors. |
| `providers.py` | The provider account facts (§10.3). |

The structures that matter most:

```python
@dataclass(frozen=True)
class SiteNetwork:
    # One subnet the gateway serves, and the gateway's own leg on it.
    name: str
    v4: IPv4Network
    vlan_id: int | None = None          # None: the untagged LAN
    gateway_v4: IPv4Address | None = None

    @property
    def v6(self) -> IPv6Network:
        # Numbered out of SITE_ULA after the v4 subnet's third octet,
        # spelled as those same digits. The rule, not a second literal.
        ...
```

A site network's IPv6 prefix is numbered after the third octet of its IPv4
subnet, spelled as those same digits — that is the site's addressing rule, so
the prefix is *derived* rather than declared beside the subnet and a pair that
disagrees cannot be written. Today the rule is applied by hand at six spellings
of the site prefix: five in `conventions.py`, and the IoT VLAN's in
`gateway/unifi.py`, a different file from the IPv4 sibling it must agree with.

The same treatment gives the `lan` pool its address groups and its fixed VIPs in
one object, and the gateway its service census:

```python
@dataclass(frozen=True)
class BridgedService:
    # A service on the container VLAN: it has an address, and may serve a name.
    name: str
    address: IPv4Address
    vhost: str | None = None

@dataclass(frozen=True)
class HostNetworkService:
    # A service in the host's own network namespace: no address of its own,
    # and therefore nothing the gateway can proxy to.
    name: str

ContainerService = BridgedService | HostNetworkService
```

Two shapes rather than one with optional fields, for the same reason the roster
has two (§10.2): a service with no address that nonetheless serves a public
name is a combination the gateway could not honor — there would be nothing to proxy to —
and this way it cannot be written. The `vhost` field carries a comment pointing
at dns.md §4, for why a name the gateway serves is a name in the public zone
that public resolvers do not answer for.

### 10.2 The overlay roster carries identities

**Operator ruling, 2026-08-28:** a node id is minted by the device and never
changes — an identity, not configuration — and a roster entry must match one
concrete instance on the network. So the roster carries the node id *and* the
address for every member, and four things follow:

1.  **`zerotierMembers` configuration retires.** Its ten entries move into
    `ROSTER` as code.
2.  **`parse_members` becomes roster validation.** There is no longer an untyped
    mapping to cross-check against the roster; what remains is the roster's own
    invariants — names unique, node ids unique, addresses unique and inside the
    overlay subnet, the gateway's entry at the address every client dials, the
    roster no larger than what multicast reaches. It runs as a test and nowhere
    else: the roster is static code, so nothing can break an invariant at
    runtime that a test did not already catch.
3.  **The `udm` entry is added at ceremony step 2, as a commit.** The gateway is
    the one member whose identity this program's own work creates: the daemon is
    a container service on the device, and its node id does not exist until the
    first delivery has run. Step 2 of the bring-up ceremony (gateway.md §2.5)
    reads that id off the device; recording it is an edit to the roster rather
    than to stack configuration. Until then the entry is absent, and absent is a
    state the code can read: no member is declared for the gateway, and no
    `udm.zt` record is published. `gatewayBootstrapHost` therefore loses one of
    its two jobs and keeps the other — it is now only the answer to "where does
    the device answer today".
4.  **The `zerotier_addresses` export and the `dns` StackReference that reads it
    retire.** Both stacks read the addresses from the roster, as code, the way
    they already read the roster itself. The StackReference exception shrinks
    back to the cluster anchors, which is what it was recorded for.

The roster's two shapes make a whole class of mistake unrepresentable rather
than checked at runtime:

```python
@dataclass(frozen=True)
class EnrolledMember:      # a device that minted its own identity
    name: str; node_id: str; address: IPv4Address; role: Role; note: str = ''

@dataclass(frozen=True)
class GeneratedMember:     # an identity this program creates in state
    name: str; address: IPv4Address; role: Role; note: str = ''

RosterEntry = EnrolledMember | GeneratedMember
```

A generated member with a configured node id is now a type error; today it is a
runtime refusal in `parse_members`.

### 10.3 Provider account facts

The cloud region and tenancy are claimed twice today — once in `conventions`,
once in the provider's ambient configuration namespace — and the backup
account's region sits in stack configuration with an argument ("an account
property, permanent") that is the argument for `conventions`.

With every provider explicit (§8), the question answers itself: the facts that
identify an account are conventions, the secrets that authenticate to it are
configuration, and the provider is built from both at one line.

```python
@dataclass(frozen=True)
class OciTenancy:
    region: str
    tenancy_ocid: str
    user_email_domain: str
    compartments: Mapping[str, Compartment]

@dataclass(frozen=True)
class B2Account:
    region: str
```

Both providers' ambient namespaces retire entirely — not "retire as readers",
but stop existing: with default providers disabled there is nothing left to
configure through them, and what remains in stack configuration is three secrets
for one account and two for the other, read where each provider is built (§8.3).
The identity-domain endpoint and name that used to sit beside these are gone
too, for a different reason: they were read only to declare the chunk store's
user, and the chunk store is deleted (§10.5). The credentials scripts keep their
own copy of those two facts, which is where they belong — they are how a mint
talks to the domain, not how this stack declares anything.

### 10.4 `facts.py` folds into configuration reading

`gateway/facts.py` is two functions that turn `require_object`'s untyped result
into typed values with a named error. That is not a gateway concept: it is how
this repository reads configuration, so it becomes `lib/config.py` and is used
by every stack that reads an object. The validation stays at the boundary and
still reports which entry is wrong by name; only the home changes, and the
gateway stops owning a general mechanism.

### 10.5 Per-node capabilities

The object-storage chunk bucket and the JuiceFS mount in front of it are
**deleted, not moved**: the one dataset behind them sits on a plain block volume
on a cloud node instead, so `ChunkStore` goes, and the credential it minted goes
with it. That is what puts a second block volume on the fleet, and a second
volume is what this subsection is about.

The design today calls one of the three cloud nodes **augmented**, and that one
word carries two capabilities that have nothing to do with each other:

1.  a **dedicated VIP** — a secondary private address and the reserved public
    address that is mapped onto it, which exactly one node has;
2.  an **attached block volume** — of which any node may have one, for a dataset
    that must outlive a container.

They were one name because one node happened to have both. A second volume
consumer makes the bundle wrong rather than merely coarse, so it splits into two
structures in `conventions`, each declared once and read three times:

```python
@dataclass(frozen=True)
class FollowsDedicatedVip:
    # This volume's node is whichever node holds the dedicated VIP.

FOLLOWS_DEDICATED_VIP = FollowsDedicatedVip()


@dataclass(frozen=True)
class NodeVolume:
    # A block volume attached to one node, and where that node mounts it.
    node: str | FollowsDedicatedVip
    size_gb: int
    mount: str

NODE_VOLUMES: Mapping[str, NodeVolume] = {...}
DEDICATED_VIP_NODE = ...
```

**One volume follows the VIP, and says so in its own declaration.** Splitting
the bundle would otherwise drop a dependency the bundle was carrying implicitly:
the dataset behind today's volume needs to be on the node that holds the
dedicated VIP, because the workload's traffic must leave by the address it
arrives on, and its cache is pinned to that machine. Two independent structures
permit moving the VIP and leaving the volume, and nothing would notice until
cutover day. So the requirement is a value the volume carries rather than a fact
about a workload: that entry's `node` is `FOLLOWS_DEDICATED_VIP`, a sentinel
type rather than a magic node name, and it resolves to `DEDICATED_VIP_NODE` on
every read. Three consequences, and the second is the one worth the machinery:

1.  **The wrong state cannot be written.** For a following volume there is no
    declarable "the VIP moved, the volume stayed"; the pair does not exist as a
    pair.
2.  **A VIP move becomes a two-step ceremony for free.** Editing
    `DEDICATED_VIP_NODE` re-declares the following volume's attachment against
    the new node, and that attachment is protected — so the volume migration the
    edit implies surfaces as a refusal until someone reconciles it, instead of a
    silent break discovered when the workload lands.
3.  **Scheduling converges by construction.** The persistent volume's
    node-affinity label and the VIP machinery are guaranteed onto the same node,
    so the workload that needs both states no placement of its own.

The weaker shape — a `requires_dedicated_vip` flag beside a node name, plus a
test — was rejected because it lets the inconsistent pair be written and only
then refuses it, where the sentinel leaves nothing to refuse.

The type does not carry the table's remaining invariants, so they are checked
the way the roster is checked (§10.2), **after** the sentinel resolves: every
`node` names a node the fleet declares, every `mount` is unique, and **no node
carries two volumes**. A volume attached to a node that does not exist, a mount
claimed twice, and a following volume that resolves onto a node another volume
already holds are then all refusals with a name on them rather than an apply
that half works.

The three readers are the three layers the volume passes through, and naming it
once is what keeps them consistent:

*   **the physical stack** attaches each volume to its node, one `NodeVolume`
    component per entry — the generalization of today's single `CacheVolume`;
*   **the day-0 machine configuration** renders each node's mounts from the same
    table, and a node label per volume, so placement is a fact about the node
    rather than a string a workload repeats;
*   **the cluster side** declares a `local` persistent volume per volume, with
    node affinity on that label. That declaration is not this document's — it is
    named here because it is the reason the label exists.

That path is what keeps the vendor in the physical layer: the cloud provider
appears where the volume is created and attached, and everything above it sees a
directory on a node.

**One volume per node, spread.** The following volume is on the VIP node by
construction; a second volume names a different node. That second placement is
the only one anybody chooses, and three reasons decide it, in order of weight:
disk selection in the machine configuration stays "the disk that is not the boot
disk" rather than a discrimination by size or serial; losing one node stops
taking two preserved datasets with it; and the load spreads. The cost is
identical either way, which is what leaves the choice free to make on those
grounds.

The word "augmented" retires with the bundle. The `augmented` parameter of the
cloud-nodes component becomes the two capabilities, passed separately, and the
node that holds each is read from `conventions` rather than handed down as one
name.

--------------------------------------------------------------------------------

## 11. What the `physical` stack reads

The rule is that configuration is read at the layer owning the concept, that
`conventions` holds decisions and identities while configuration holds what an
operator supplies or rotates, and — §8.1 — that a credential existing only to
configure a provider is read where that provider is built. Applied to every read
the stack performs today:

| Key | After |
| --- | --- |
| `talosVersion` | stack program, into the Talos components |
| `budgetAlertRecipients` | stack program — operator-supplied, rotates |
| `workerGua` | stack program — observed off a booted machine |
| `gatewayBgpPassword` | stack program — a file's content, not a provider's |
| `gatewayAcmeToken` | stack program — likewise; it configures caddy |
| the cloud account's user, fingerprint and private key | the stack program, at the line that builds the shared cloud provider |
| the backup account's key pair | inside `BackupBucket`, at the line that builds its provider |
| `unifiApiKey` | inside `Gateway`, at the line that builds the controller provider |
| `zerotierApiToken` | inside `Overlay`, at the line that builds its provider |
| `libvirtPrivateKey` | inside `HomelabHost`, at the line that builds its provider (§8.4) |
| `gatewayPrivateKey` | by the device-files provider alone, in `configure` (§7.4) |
| `gatewayBootstrapHost` | the same place — it is where that provider dials |
| `gatewayRootfs` | four `image:` pins (§11.1) |
| the cloud region and tenancy | `conventions.providers` (§10.3) |
| `b2Region` | `conventions.providers` |
| `ociIdentityDomainUrl`, `ociIdentityDomainName` | gone with `ChunkStore` (§10.5) |
| `zerotierMembers` | `conventions.overlay.ROSTER` (§10.2) |
| `zerotierNetworkId` | `conventions.overlay` — the identity of the adopted network |
| `gatewayAddresses` | `conventions.gateway` |
| `gatewayHostKey` | `conventions.gateway` |
| `haosDomainUuid` | retires — nothing declares that domain (§13) |
| `libvirtStorageDir` | `conventions.homelab` |
| `qbittorrentPeerPort` | `conventions` |

Five of those are judgment calls rather than applications of an existing ruling,
and each has a precedent in the repository:

*   **The container-VLAN addresses.** Nothing outside this program assigns them.
    For the two resolvers the program injects the address into the container's
    own environment, and three declarations must then agree on it: the caddy
    site that proxies the instance, the initial state that tells it where to
    listen, and the flow rule that admits a run to it. The caddy service is
    different and is a decision all the same — its image takes a lease, so its
    entry is the address the design *intends* it to hold, which is the one a
    resolver rewrite has to name. Either way the value is this repository's, and
    it is the same argument that already put the resolver API port in
    `conventions`.
*   **The gateway's pinned host key.** The homelab host's pinned key is already
    a constant in code, with the reason written beside it: a public key is not a
    secret, and a pin typed in beside the client credential could be replaced by
    whoever could already replace the credential. The gateway's key is the same
    kind of fact and is presently a configuration secret, which also redacts it
    from the previews where a reader would check it.
*   **The libvirt storage directory.** It is the path of a pool this program
    declares, agreed with the host's own configuration management. Both sides
    have to name the same directory, which is what makes it a convention rather
    than a setting.
*   **The peer port.** `conventions` already carries a public-port census; the
    peer port is named twice in the firewall — the pinhole and the forward — and
    belongs beside it.
*   **The overlay network's id.** It is what the network is adopted by: minted
    by the service, stable, and meaningless to change. It is not a secret
    either; joining takes an authorized member, not knowledge of the id.

What is left in stack configuration is credentials, values that rotate, values
observed off running machines, version pins, and the one ceremony knob — which
is the definition the style rules give.

### 11.1 Version pins, and one namespace for them

The four root filesystems are pinned the way container images and Helm charts
are pinned, because that is what they are: a build someone else produced,
selected by version. The repository has that mechanism already — a `versions`
object over configuration namespaces, one accessor per kind. Three things change
about it here, and the first two are cheap because **nothing uses it yet**: the
`image:` and `chart:` namespaces have no committed value in any stack, since the
stacks that would carry them are unwritten. Renaming them today costs a rename
in one module and one caller.

**One namespace, `versions:`, with the kind in the key.** Today's `image:` and
`chart:` are two namespaces for one concept, and a third kind is arriving, and a
fourth already exists in the wrong place. So:

```yaml
versions:talos: v1.13.9
versions:chart-cert-manager: https://charts.jetstack.io:v1.19.1
versions:image-adguard: docker.io/adguard/adguardhome:v0.107.68
versions:rootfs-gateway-caddy: rootfs-1:e154a141364c60cc…
```

`lib/versions.py` exposes one typed accessor per kind — `versions.talos`,
`versions.chart[...]`, `versions.image[...]`, `versions.rootfs[...]` — each
returning a parsed value rather than a string, and each refusing a missing key
by name. The kind prefix is what lets a single renovate manager per kind match
its own entries and nothing else.

**A third kind rather than reusing `image:`.** "Image" in this repository means
a container image, and these are not that: they are root filesystem archives
consumed by `systemd-nspawn`, published as release assets, and pushed to a
device that cannot pull from a registry. Calling them images would make the one
word mean two things, which is the rule this document has been applying
throughout. `rootfs-` says what they are.

**`talosVersion` joins them.** It is a version pin sitting among unrelated stack
settings today, with its own renovate manager pointing at its own key. Under
`versions:talos` it is beside its siblings and inside whatever manager covers
the namespace.

Three properties of the rootfs entries, each replacing something the current
shape gets wrong:

*   **The value is a string, not a structure.** Today it is a JSON object of
    objects, parsed by hand. A version pin is a scalar in every other place this
    repository pins one, and the parse function goes away with the structure.
*   **The URL stays out of configuration.** Which repository publishes the
    artifacts, and how a release names them, is a decision, so it is a rule in
    `conventions` applied to the release name rather than four URLs an operator
    maintains. Moving publication elsewhere is then a reviewed edit to that rule
    — which is the right weight for a change of that kind, and it is also a
    question that dissolves entirely if the artifacts become registry images
    (below), because then the reference *is* the pin.
*   **Four keys, not one.** The fact that there are four services lives in the
    component signature (§5.3); the configuration is four independent pins. The
    two resolvers normally carry the same value, and two keys is exactly what
    lets one of them move first — proving a new resolver image on one instance
    before the other is a rollout this shape permits and a single shared pin
    forbids.

**Where the keys live, given five stacks.** Version pins would otherwise scatter
across five stack configuration files, and Pulumi has no include or import
between them. It does have the thing that actually solves it: **project-level
configuration** — a `config:` block in `Pulumi.yaml` whose values apply to every
stack, with a stack's own file overriding a key it repeats. The `versions:`
namespace goes there, so one block holds every pin in the repository and a stack
overrides one only when it deliberately runs a different version from the rest.
`lib/versions.py` reads it the same way from any stack, unchanged.

Two limits of that mechanism, neither of which bites here: `pulumi config set`
cannot write project-level values, so these are hand-edited YAML — which is what
a renovate-maintained pin wants anyway — and a key in someone else's namespace
may not declare a type or a default, only a value.

Pulumi ESC was the alternative and is unavailable: an environment can only be
imported by a stack whose state lives in Pulumi Cloud, and this project's state
is in a self-managed backend. Project-level configuration costs nothing and is
already in the file that exists.

**Renovate, for the `rootfs-` kind only.** The other kinds are solved already:
the legacy repository has working custom managers for chart and image pins in
stack configuration, and this repository reuses that design — a regex manager per
kind over the file the pins live in, which is now one file rather than five.
Talos keeps the manager it already has, re-pointed at its new key. What is
genuinely unsettled is the root filesystems, because their publication format
decides what a manager can do:

**The root filesystems are published as registry images**, tagged by commit and
pinned here as a tag and a digest. That is what makes the pin maintainable: one
data source bumps tag and digest together in the documented `tag@sha256:…` form,
the digest costs a registry request rather than a download, and the pins stop
being a kind of their own — the `rootfs-` prefix exists only until the
publication changes, after which these are `image-` entries like any other.

Two facts drive it. A **commit-hash tag** is what makes each build addressable
without inventing a version stream for images that have none. A **registry
digest** is the only artifact identity renovate maintains natively end to end.

The build side is the image repository's work; specifying the format is this
document's, and the two are sequenced in §15.

**Alternative considered: keep release assets, and publish checksums beside
them.** Renovate has a data source that resolves a release asset's own sha256
from the release's checksum manifest, so today's shape *can* be maintained
automatically. It was not chosen, because it constrains publication in four ways
where the registry constrains it in none:

| | Release assets with checksums | Registry images |
| --- | --- | --- |
| Tag and digest in one place | required to be one file, one line | native to the reference |
| Asset naming | filename must contain the version | free |
| Hash format | raw sha256 or sha512 only | native |
| Cost of a check | downloads assets unless a `SHA256SUMS` asset exists | one registry request |

The third option, pinning by commit through the git-refs data source, was
rejected for a different reason: it maintains a hash against a moving branch,
and nothing in it knows whether the build for that commit ever produced an
artifact.

--------------------------------------------------------------------------------

## 12. One shape per role

A stack program builds a handful of **top-level components** — the cloud site,
the Talos chain, the homelab host, the gateway, the overlay, the backup bucket —
one per area of `components/` that the stack has anything in. Today those seven
things are built in four different shapes: a component constructed directly, a
module function returning a component, a module function returning nothing, and
a stack-private helper. The style rules ask for one.

**A top-level component is a class, and the stack program constructs it.** No
`declare_*` wrapper exists anywhere: a wrapper is a second name for the same
thing, it hides the component from the reader, and in one case it throws the
component away by returning `None`.

The stack program keeps a private helper only where a component needs several
configuration reads to become one constructor call; the helper is named for its
component and returns it. **Exports move into one block** in the entry point —
today they are scattered across four functions, and they are this stack's whole
contract with `dns`, `k8s-base` and the credential machinery, so they are worth
reading in one place.

--------------------------------------------------------------------------------

## 13. The home-automation domain, which this program does not touch

*Libvirt calls a virtual machine a **domain**, and this section uses the word in
that sense only.*

**The `physical` stack declares nothing about the home-automation domain.** The
import that adopts it today is deleted, along with its ignore list and the
configuration key that names it.

The test any adoption has to pass is whether an apply can put the machine back
after someone removes it. Four ways of passing it were measured against the live
definition and the libvirt provider, and each fails differently:

| Option | Detects drift in | Recreates | Why not |
| --- | --- | --- | --- |
| Declare what the provider reads | the read-back fields | most of the machine | blind to the passthrough hardware, the TPM and the loader — the parts that make it this machine |
| The same, plus an XSLT transform | the read-back fields only | the machine, if the transform is faithful | the transform's own contents are never read back, so it is blind exactly where it acts |
| Full XML in the repository, provider owns lifecycle | nothing, through Pulumi | the machine | after an external removal the resource leaves state and the next apply *creates* from the lifecycle fields alone — an empty machine wearing the name, and `protect` does not guard creates |
| Define it ourselves over SSH | the whole definition | the machine | real, and a bespoke mechanism to own for one machine that predates and outlives this program |

The last row is worth recording rather than dismissing: a dynamic resource whose
input is the full XML, whose create is `virsh define` and whose diff is a
normalized `dumpxml` comparison would give complete drift detection. What rules
it out is not capability but ownership — a bespoke mechanism, maintained here,
for a machine this program neither creates nor depends on.

**Where it lives instead.** The domain's full XML is managed by the host's own
configuration management, beside the rest of that host's preparation — the
mechanism that already manages this machine's definition today, and the one that
will still be there when this program is replaced. Recreation is a host-side
`virsh define` of that file. Drift detection is a comparison of a normalized
`virsh dumpxml` against it, and belongs to the operational drills rather than to
Pulumi state.

Two things follow inside this document: the domain's identifier retires from both
configuration and `conventions` (§11), and the slice that would have declared it
becomes a deletion (§15). The worker VM is unaffected — it is created by this
program, it is declared through the libvirt provider, and everything §8.4 says
about that provider's session stands.

--------------------------------------------------------------------------------

## 14. What the tests look like after

The shared test machinery is settled elsewhere; this section says only what the
work described here leaves for it.

*   **The gateway's suite re-splits along the new component boundaries.** It is
    four files today — the estate, the provider, the controller resources, the
    overlay — and stays about that many, but the cuts move: the services and
    their containers, the site firewall, the device-file provider, and — now in
    another area entirely — the overlay and its rule composition.
*   **The rendered-text cases survive unchanged.** The render functions keep
    their names and signatures across the move to templates (§9.2), so the cases
    that assert on fragments of a unit file, a routing configuration, a caddy
    configuration or the recovery script keep asserting on what they assert on
    now. That is the point of keeping the functions.
*   **Three census tests become type-level and disappear**: a pin with no
    service, a service with no pin, and a resolver bound to a service that has
    no address (§5.3). What replaces them is the version mechanism's own
    missing-key error.
*   **The roster gains an invariants test** — unique names, unique node ids,
    unique addresses inside the overlay subnet, the gateway at the address
    clients dial, the roster within the multicast limit — which is what is left
    of `parse_members`.
*   **The adopted domain's tests go with the declaration.** The ratchet that
    accounted for every ignored libvirt input retires with the import it
    guarded (§13). What stays is the worker VM's own coverage, which never
    depended on it.
*   **Several assertions on stack configuration keys retire** with the keys
    (§11), and the site-fact refusal tests shrink to the keys that remain.

--------------------------------------------------------------------------------

## 15. How we get there

Eleven slices, in this order, each an issue cut from this document. The order is
chosen so that nothing is moved twice: homes first, then the data every layer
reads, then the text, then the structure that consumes both.

1.  **The layout move.** The tree of §2.2, as renames with no behavior change,
    plus the `lib/workstation.py` split and the import contract of §2.3 in CI.
    The `dns` and `github` packages move with it and are otherwise untouched.
2.  **`conventions` restructured** (§10.1, §10.3, §10.5): the package, the
    structures, the provider account facts, the per-node capabilities. Readers
    follow; no vocabulary changes yet.
3.  **The roster carries identities** (§10.2): `zerotierMembers`, the
    `zerotier_addresses` export and the `dns` StackReference retire together,
    because each is the other's only remaining reason to exist.
4.  **The parent backstop** (§8.2), in `putils`: the context variable and the
    transformation that refuses an unparented resource inside a component. It
    comes before the provider slice because that slice's correctness depends on
    parentage, and it is framework code with its own tests.
5.  **Providers, explicitly** (§8): every provider built where its credentials
    are read and set on its top-level component, default providers disabled, the
    ambient namespaces gone, and the libvirt URI's paths made relative. This one
    is worth its own live drill: it changes how every resource in the stack is
    authenticated.
6.  **Rendered configuration from files** (§9): the mechanism, then the eight
    literals, with the render functions' signatures unchanged.
7.  **The gateway component tree** (§4, §5): `Gateway`, `DeviceServices`,
    `Container`, the typed declarations, the units' own dependencies, and the
    vocabulary of §3 applied throughout.
8.  **The overlay leaves the gateway** (§6): `components/overlay/`, the rule
    composition as a pure function, the stack program composing it.
9.  **The device-file provider** (§7.4): the provider made stateless, the
    connection moved into `configure`, and the two properties `check` injects —
    the session fingerprint and the version — that make a rotation and a code
    change visible without the program touching either.
10. **The stack program** (§11, §12), then the caddy certificate (§9.3).
11. **The home-automation domain's declaration is deleted** (§13): the import,
    its ignore list, the configuration key and the ratchet test that guarded
    them. Free to do now, and only now — the import has never been applied, so
    removing it moves nothing on the host.

Two things alongside, neither of them this repository's to implement:

*   **The root filesystems' publication moves to registry images** (§11.1).
    Until it does, the pins stay manual.
*   **The home-automation domain returns to the host's configuration
    management** (§13), where it is carried by the same tooling as the rest of
    that host's preparation. It is not a revert of the change that removed it:
    the domain's disk image has since moved onto the nodatacow subvolume, so the
    restored definition carries the new disk path, and the entry that creates
    that subvolume stays. Host-side work, sequenced with slice 11.

Slices 1 to 3 are mechanical and reviewable by diff; 4 to 11 each rewrite their
own tests. None of them is a migration: the `physical` stack has no state, so
every rename in this document is a rename and not a replacement.
