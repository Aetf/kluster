# RFC 002: Source Layout, and the Gateway as a Component Tree

*   **Status:** Proposed. It is approved before implementation starts, and the
    implementation issues are cut from the accepted text.
*   **Created:** 2026-08-28
*   **Authority:** the style rules (`docs/style/`) are what this document
    obeys; where they are silent, a rule proposed here is marked **new rule**.
*   **Companion:** rfc-001 is the async-input framework and stays untouched.
*   **In scope:** the tree under `src/`; how the gateway device runs what it
    runs, and how that is declared; the overlay; talking to systems Pulumi has
    no provider for; providers and their credentials; rendered configuration;
    the `conventions` restructure; and the `physical` stack's configuration
    reading, API shape and adopted domain.
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
| Shared helpers | `kluster/lib/`, `putils` | Code with no resources and no domain. |

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

The asymmetry between those two is the whole of the next paragraph: a bridge is
a network device, and **systemd creates device units only for kernel devices
tagged `systemd` in the `udev` database — by default all block and network
devices
"and a few others"**. So `br5` has a real device unit to bind to, and
`/dev/net/tun` does not.

The overlay daemon therefore gets **no** device dependency, but it does get a
cheaper guard, and the reasoning is recorded so that nobody re-opens it blind.

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

The established practice on these boxes is worth knowing here, because it sets
the bar: the community recipe runs the daemon under `podman run
--device=/dev/net/tun --net=host`, launched from a numbered script in the same
persistent boot directory this design uses, with no systemd unit at all and
therefore no ordering, no assertion, and no restart policy. Running it under a
unit is already more structure than the norm.

The interface the daemon itself creates is not a candidate either: a unit cannot
wait on a device its own service brings into being.

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
    `refresh` and `destroy` need `--run-program`, because the implementation
    lives in the program (`destroy` since Pulumi 3.160.0, `up --refresh` since
    3.169.0); every resource stores a serialized copy of the provider in state,
    marked secret since 3.75.0; the package half of the type token is always
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

**Recommendation: keep it, deliberately.** The property that decides it is drift
detection against the device. What makes this convergence rather than
record-keeping is that `diff` opens a session and compares the device's bytes
with the declared ones, so a file someone edited on the box appears in
`pulumi preview` without a refresh. The Command provider's `diff` never looks at
the target, so it cannot express that; a full provider could, at the cost of a
Go codebase to maintain for one device.

Pulumi's three criteria are met exactly: no provider covers this, the logic is
specific to this program, and nothing else will ever consume it. The costs are
accepted with their consequences named:

*   **No `pulumi import`.** Nothing here needs it: the resources are created by
    this program, and a device that already has the files converges to the same
    content on the first apply rather than needing adoption.
*   **`read` is real but is not the primary mechanism.** Since Pulumi 3.216.0 a
    refresh calls `read` and feeds its returned inputs into the diff, so the
    implementation here is live rather than dead code. `diff` remains what a
    preview uses, which is why drift shows up without asking for a refresh. Both
    paths need `--run-program`.
*   **A serialized provider per resource.** Roughly a dozen resources on one
    device; the state cost is noise.

If a second device ever appears, or another repository wants this, the
conclusion changes and a full provider becomes worth its pipeline. That is the
trigger to re-open this, and it is written here so the re-opening is a decision
rather than a surprise.

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

### 7.4 Connection state, and what a diff must still catch

**A native provider has a resource; a dynamic one does not.** That difference is
the whole of this subsection, so it is worth stating exactly. When a program
builds a `kubernetes.Provider`, that provider *is* a resource: it appears in
state with its configuration as properties, and changing the cluster it points
at is a diff on a named object that a preview shows. Pulumi state is part of the
actuated state, and the connection is part of what state records.

A dynamic provider has no such object. `pulumi.dynamic.ResourceProvider` is a
plain Python class, not a `ProviderResource`; there is nothing to register and
nothing for `opts.provider` to point at (§7.1). What Pulumi does instead is
serialize the provider *instance* into a reserved property on **each resource it
manages**, `__provider`, and mark it secret. So the connection has no
representation of its own in state: it has N hidden, unreadable copies.

Everything below follows from that. Pulumi's own guidance is that passing
credentials as resource inputs is an antipattern — they belong to the provider,
out of band. Today all five connection properties are inputs on every file and
artifact resource. They move onto the provider instance:

```python
# inside Gateway, which owns the connection and therefore reads its credential:
self.session = DeviceSession(
    host=..., host_key=conventions.gateway.HOST_KEY,
    private_key=pulumi.Config().require_secret('gatewayPrivateKey'),
)
self.device = DeviceFileProvider(session=self.session)
```

`Gateway` then hands that provider instance to each `Container` and to the two
files it declares itself. That is as close to inheritance as the mechanism
allows — a dynamic provider cannot travel through `opts.provider` (§7.1), so it
travels as an ordinary Python object down the same tree the components already
form. Pulumi serializes it into each resource's `__provider` property and marks
that secret, so the credential is better protected there than as an input.

Two properties the current design relies on have to survive that move:

1.  **A rotated credential or a moved address must be a change.** Calling a
    rotation "no change" because the file on the device is already right leaves
    the superseded key in state, and a delete months later would authenticate
    with a key that no longer opens the door.

    The serialized provider does move when the session does — the pickle is
    the *instance*, and the class travels by reference, so provider-code edits
    leave it unchanged — which makes comparing it technically sufficient. It is
    still not what this design does, for a reason that is about the reader
    rather than about correctness: `__provider` is a secret base64 blob, so a
    preview would report "one opaque property changed" and no human could tell a
    key rotation from an address move from a bumped dependency.

    Instead, each resource declares one input the provider derives from its
    session: a readable endpoint (`user@host:port`) and a short digest over the
    two credentials. The diff then *says* what happened. It is one small
    property instead of five, it names the session that wrote the file, and it
    carries no secret.
2.  **The pinned host key must stay reviewable.** It is a public key, and a pin
    a reader can check beats a pin the engine redacts. It becomes a convention
    rather than a configuration secret (§11), which is where the homelab host's
    pinned key already lives, and `Gateway` registers it as one of its own
    component outputs so the value a deployment trusts is stated once rather
    than on a dozen resources.

**How the data flows, concretely.** The provider instance is built first, inside
`Gateway`, and passed to each resource's constructor as its first argument —
which is how a dynamic resource takes a provider anyway. The session input is
then derived, not declared: `DeviceFile` and `DeviceArtifact` share a base class
whose `__init__` reads `provider.session`, computes the endpoint-and-digest
value, and puts it in the property bag before handing it to
`dynamic.Resource.__init__`. **A caller therefore declares nothing about the
session** — it passes the provider, as it must anyway, and the derived property
appears. That is the recommendation: one place computes it, no call site can
forget it, and the two resource classes cannot disagree about how it is spelled.

**The alternative considered: a stateless provider plus a session resource.**
The appealing version of this is not merely cosmetic. If the connection lived on
a `DeviceSession` *resource* — its secret marked secret there, once — and the
provider looked it up while provisioning, then the provider instance would carry
no connection at all, would be identical for every resource, and would never
change when a credential rotated. The rotation would be a diff on one visible
object, and the pickled blob would stop moving.

**It is not implementable, for a reason in the provider interface rather than in
this design.** A dynamic provider's methods receive exactly one thing: the
property bag of the resource being provisioned. `create`, `diff`, `update`,
`read` and `delete` are handed that resource's own inputs and state, and nothing
else — there is no handle to the engine, no state-lookup call, and no way to
reach another resource, in the parent chain or anywhere else. `configure`
adds only stack configuration. So connection material can reach the provider by
exactly two routes: captured on the provider instance, which is what this design
does, or declared as inputs on every resource, which is what it is moving away
from. A session resource could hold the values, but the file resources would
have to take them back as inputs to make them reachable — reintroducing N copies
of the secret, which is the thing being fixed.

The cost of the route taken is the one named above: the connection has no
visible object, so the derived session input is what makes a rotation legible.
If the dynamic-provider interface ever grows a way to read another resource's
state at provision time, this is the design to revisit, and the session resource
is what to revisit it with.

The resource identifier becomes the path on the device alone, a provider
instance now standing for exactly one device. It is minted at create, never
re-derived afterward, and never a replace trigger: only the path is a
replacement, as today. That is what keeps the first-bring-up knob safe — moving
the session from the device's LAN address to its overlay one (gateway.md §2.5)
stays an update to the same resources, and never a delete and a create.

The same shape applies to the third dynamic provider in the repository, the one
that writes AdGuard rewrites: its endpoint, username and password are inputs on
every rewrite today. Fixing it belongs to the `dns` document; the rule is
stated here because it is one rule.

### 7.5 Where the providers live

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
    the
    overlay's administration token belongs to `Overlay`; the libvirt session
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

**New rule**, extending rather than contradicting the style rules. They already
say that a provider which is an implementation detail of one component is
constructed inside it and that a shared one is constructed by the owner of the
connection; what is new is (a) that the credential is read at the same place
rather than passed in, and (b) that "the owner of the connection" for a provider
with several consumers is the stack program.

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
    configured by ambient namespaces today, which is configuration acting at a distance: the same
    program run with a different ambient environment declares against a
    different account. `pulumi:disable-default-providers`, listing the packages
    this program uses, turns "somebody forgot the provider" from a silent
    fallback into an error. It lists them rather than saying `*` because
    dynamic resources *depend* on the `pulumi-python` default provider: `*`
    would disable the one default provider this program still needs.
*   **A credential that configures a provider reaches no component's
    signature.** Not the component that builds the provider — it reads it — and
    not any component below.

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

Unrelated, and worth separating because the two were confused: the invoke rule
in §8.1 is about *provider* inheritance for a function call, not about parents
of resources. An invoke takes a parent only to find that parent's provider.

### 8.3 Every provider this stack uses

| Provider | Built by | Reaches | Credential, read at that line |
| --- | --- | --- | --- |
| cloud | the stack program | set on the cloud, Talos-image and node-volume components | the account's user, fingerprint and private key |
| controller | `Gateway` | set on its `SiteFirewall` child | the controller API key |
| device files | `Gateway` | its own children, as an object (§7.4) | the device's SSH private key |
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

The device-files row differs in *mechanism* but not in rule: `Gateway` reads the
key and builds the session and the provider together, and the "reaches" column
means an object passed down the same tree, because the mechanism cannot carry a
dynamic provider through resource options (§7.1).

### 8.4 The libvirt transport's absolute paths

The libvirt provider is configured by a URI, and this program builds that URI
from two files it writes into the checkout: the client identity and a one-line
`known_hosts` carrying the pinned key. Both go in as **absolute** paths, so the
provider's configuration — which is a resource input, kept in state — contains
the path a particular machine happened to have. Run the same program from a
checkout at another path and the URI differs, so the provider diffs.

It is noise rather than danger: the bridged provider does not mark `uri` as
forcing a provider replacement, so the diff is a provider update and the adopted
domain is not at risk from it. But it is a diff that can never be resolved, on a
stack where a clean preview is the merge gate.

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
clears its id, which for a protected, adopted domain is the worst-shaped failure
in this stack.

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
| `homelab.py` | The worker node's name, address and sizing, the host bridge, the host's pinned key, the adopted domain's UUID. |
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
| `gatewayPrivateKey` | inside `Gateway`, at the line that builds the device session (§7.4) |
| `gatewayBootstrapHost` | the same line — it is where that session dials |
| `gatewayRootfs` | four `image:` pins (§11.1) |
| the cloud region and tenancy | `conventions.providers` (§10.3) |
| `b2Region` | `conventions.providers` |
| `ociIdentityDomainUrl`, `ociIdentityDomainName` | gone with `ChunkStore` (§10.5) |
| `zerotierMembers` | `conventions.overlay.ROSTER` (§10.2) |
| `zerotierNetworkId` | `conventions.overlay` — the identity of the adopted network |
| `gatewayAddresses` | `conventions.gateway` |
| `gatewayHostKey` | `conventions.gateway` |
| `haosDomainUuid` | `conventions.homelab` |
| `libvirtStorageDir` | `conventions.homelab` |
| `qbittorrentPeerPort` | `conventions` |

Six of those are judgment calls rather than applications of an existing ruling,
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
*   **The adopted domain's UUID.** It is minted by libvirt on the host and never
    changes — the same shape as a node id, under the same ruling.
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

## 13. The adopted HAOS domain

*Libvirt calls a virtual machine a **domain**, and this section uses the word in
that sense only.*

Today every input the libvirt provider accepts for a domain is ignored, so
Pulumi owns one fact: that a domain with this UUID exists. That fails the
question adoption has to answer — **if somebody deletes the domain outside this
program, can an apply put it back?** As things stand, no. And if it cannot, the
adoption is buying nothing.

The honest answer is more complicated than the previous revision of this
document claimed, because it depends on what the provider can express about
*this* domain. What follows is measured against the live definition and the
pinned provider, and then the choice is put to the operator.

### 13.1 What the provider can and cannot say about this machine

**What the provider can own, and reads back, so declaring it costs no diff on
adoption:** `name`,
`description`, `vcpu`, `memory`, `firmware`, `nvram`, `cpu`, `arch`, `machine`,
`autostart`, `running`, the disks, and the network interface. Two details decide
whether that is true in practice:

*   **Disks must be declared by `volume_id`, not by `file`.** The provider reads
    a file-backed disk back by looking its path up in a storage pool and
    reporting the volume it found; a declaration written as a file path would
    diff against that on the first refresh, and the field forces replacement. It
    also means the disks can only be declared **once a pool covers the directory
    they live in** — the lookup errors on a path in no pool.
*   **`machine` is the canonical string the host reports** — `pc-q35-8.1`, not
    the family name `q35`. Declaring the family would diff forever.

**Expressible only through the XSLT escape hatch:** the passthrough hardware.
The provider has no `hostdev` input at all, and this domain has three: two PCI
functions bound to vfio, and one USB device. They exist in the definition or the
machine is not the same machine.

**Not expressible at all:**

*   **The TPM.** The provider has a `tpm` block, but it never populates it on
    read, so a declared one is a permanent diff. This domain has an emulated
    TPM whose state lives in a directory on the host — and that state is
    identity-bearing: it is what the guest's disk encryption and attestation are
    tied to.
*   **Secure boot.** A definition the provider generates asks for a firmware
    loader with `secure="no"` and emits no `<smm>` element, so the loader this
    domain actually boots is not what a recreation would produce.

**Which owned fields refuse, and which quietly do nothing**, is the correction
that matters most to the previous revision, which claimed everything but two
fields would refuse:

*   `name`, `description`, `vcpu` and `memory` force replacement. Under
    `protect=True` a drift in them is a **refusal until someone reconciles it**,
    which is the intended behavior.
*   `machine`, `arch`, `emulator`, `video`, the network interfaces, boot
    devices, `kernel`, `initrd`, the agent flag and `metadata` are **not**
    replacement-forcing, and the provider's update path does not apply them
    either. Drift there is a **silent no-op**: Pulumi reports the update, the
    host keeps what it had. Declaring them is worth doing for what it documents
    and for what a recreation emits, but it must not be sold as enforcement.
*   `consoles`, `graphics` and `video` are never read back at all. This domain
    is headless — a virtio console and no graphics — so there is nothing to gain
    by declaring them and a permanent diff to lose.

### 13.2 What each option detects, enforces and recreates

Four ways to make adoption mean something, compared on the three things that
matter. "Detects" means: appears in `pulumi preview` without anyone running a
separate procedure.

| | Detects drift in | Enforces | Recreates after an external delete |
| --- | --- | --- | --- |
| **(a)** declare what the provider reads, transform for passthrough | the read-back fields | 4 fields refuse; the rest silently no-op | most of the machine, without TPM or secure boot |
| **(b)** full XML in the repository, provider owns lifecycle | nothing | lifecycle only, plus `protect` | **defines an empty machine** — see below |
| **(c)** (a) plus TPM and loader in the transform | the read-back fields — **not** what the transform injects | as (a) | the machine, if the transform is faithful |
| **(d)** define the domain ourselves over SSH | **the whole definition** | the whole definition | the machine, from the checked-in definition |

Three findings decide it.

**The provider never sees the full XML.** In (a) and (c) it composes a
definition from its own inputs plus the transform; in (b) it is not given the
XML at all — the file is applied by `virsh define` outside Pulumi, and the
provider declares only `autostart` and `running` on the domain it adopted. At
preview time, it reads the live domain into the fields its own read implements
and no others, which is why the "detects" column is what it is.

**(b) has a footgun, and it is the original failure automated.** If the domain
is deleted on the host while its disks survive, the provider's read finds
nothing, clears the resource's id, and the resource leaves state. The next `up`
then *creates* it — from the declared inputs, which in (b) are the lifecycle
fields alone. The result is a domain with no disks, no passthrough and no TPM,
wearing the right name. `protect` does not help: it guards deletion and
replacement, not creation. So (b) is only safe as a written procedure —
`virsh define` first, refresh second, apply third — and an ordinary
`up --refresh` run in the wrong order produces the empty machine.

**(d) removes the reason the others are partial.** The provider is not asked to
model a domain at all; it is asked to make the host's definition equal a file:

*   **inputs**: the full domain XML, checked in and rendered by the mechanism of
    §9, plus the two lifecycle flags;
*   **create**: `virsh define`, then autostart and start. Adoption needs no
    import — defining over an existing domain replaces its persistent
    definition, and if the file was generated from that domain the change is
    nil;
*   **diff**: normalized `virsh dumpxml --inactive` against the declared XML.
    The persistent definition is the one compared, because that is the one
    `define` writes; a running domain keeps its live definition until it next
    boots;
*   **update**: define again;
*   **delete**: refused while protected; otherwise `virsh undefine`, which
    leaves storage alone;
*   **after an external delete**: an ordinary diff, and the next apply defines
    the machine back around its surviving disks. Automatically, and completely,
    because the file is complete.

### 13.3 The recommendation, and what it costs

**(d), for the adopted domain.** It is the only option that detects drift in the
passthrough devices, the TPM and the loader — the parts that make this machine
that machine — and the only one whose recreation path is the same code path as
its steady state. It also removes the second source of truth that (c) was
rejected for: there is one definition, in the repository, and the diff is
against the host.

Three costs, stated plainly:

*   **We own the normalization.** `dumpxml` emits more than `define` was given:
    security labels, device aliases, and address elements libvirt assigns. The
    answer is to check in the **fully expanded** definition — what the host
    reports today, not a hand-minimal XML — so that most of that is already in
    the file and the remaining volatile subtrees are few. Which subtrees those
    are is established empirically in the slice that lands this, by defining and
    dumping until the round trip is stable; the RFC does not guess at the list.
*   **The worker VM stays on the libvirt provider.** Its declaration creates a
    volume from a local image file, which means uploading a gigabyte into a
    pool — a provisioning operation `virsh` over an SSH channel would have to
    reimplement badly. So the boundary is: the adopted domain is defined by us,
    the created VM is declared by the provider. The libvirt provider therefore
    does *not* leave the stack, and §8.4's relative-path work stands.
*   **A second SSH session, to a second host.** The device-files provider family
    grows an instance pointed at the homelab host, authenticating as the same
    service account the libvirt URI already uses. That is one more session to
    hold, and it is the same shape as the gateway's.

The alternative if (d) is judged too much machinery is **(b) plus a drift
drill** — the full XML as artifact, a scheduled `dumpxml` diff for detection,
and the define-then-refresh procedure written down where whoever recreates the
domain will find it. It gets the same coverage as (d) at the price of the
footgun above and of detection that only runs when the drill does.

The ratchet that guards the adopted domain today — every input the libvirt
provider accepts is accounted for, so a provider release that grows a field
fails a test rather than silently proposing a change to a running Home
Assistant — **retires with the provider** under (d): there is no longer a
libvirt resource for that domain to grow fields on. What replaces it is
narrower and stronger: the diff itself is the check, and the test that matters
is the round-trip one — define the checked-in definition into a scratch domain,
dump it, and assert it normalizes equal to the file. The ratchet stays where the
worker VM is declared, which is still the provider's.

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
*   **The adopted domain's tests move with its mechanism.** The libvirt ratchet
    stays for the worker VM, which still uses that provider. The adopted domain
    gains a round-trip test instead: the checked-in definition, defined into a
    scratch domain and dumped, normalizes equal to the file it came from (§13.3)
    — which is what makes the normalization rules a fact rather than a hope.
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
9.  **The device-file provider** (§7.4): the connection onto the provider
    instance, the derived session input, the pin as a component output.
10. **The stack program** (§11, §12), then the caddy certificate (§9.3).
11. **The adopted domain** (§13), last and alone: it needs a ruling on §13.2
    first, and under the recommended option it also needs the round-trip
    normalization established against the live machine before anything is
    declared.

Alongside, and not this repository's to implement: the root filesystems'
publication moves to registry images (§11.1). Until it does, the pins stay
manual, which is the one thing in this document that a merge here cannot
finish.

Slices 1 to 3 are mechanical and reviewable by diff; 4 to 11 each rewrite their
own tests. None of them is a migration: the `physical` stack has no state, so
every rename in this document is a rename and not a replacement.
