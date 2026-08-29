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
| Stack programs | `kluster/stacks/` | Wiring. Reads configuration, builds providers and top-level components, exports outputs, declares no resource of its own. |
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
is decided by *files* rather than by a probe. The script keeps a stamp beside
each service's state: a checksum over the unit, the digest marker of the root
filesystem tree, and every configuration file the container mounts. Unchanged
stamp plus an active unit means nothing to do. There is no health check on the
inside and no monitor: `Restart=always` in the unit is what handles a service
that dies, and systemd is the thing that notices. The script decides only
whether the *definition* moved.

The digest marker rather than the tree itself, because walking a root filesystem
to learn it has not changed costs more than the restart it would save. The
marker beside the *tree* rather than beside the archive, because the archive's
marker is written after the hook has already run — a service that waited for it
would learn about a new root filesystem one deployment late.

### 4.3 What the units express

Each service's unit states its own requirements, and the recovery script chooses
no start order:

*   every service wants and comes after `network-online.target`;
*   a service on the container VLAN binds to, and comes after, the bridge's
    device unit — so a container cannot be started against a bridge that does
    not exist yet. Today that race is absorbed by `Restart=always`, which is a
    retry loop standing in for a dependency.

The overlay member gets **no** device dependency, and the reason is recorded
here so that nobody adds one later. What it needs is `/dev/net/tun`, a
miscellaneous character device that the device's stock rules do not tag for
systemd: the matching device unit is loaded but never becomes active, so binding
to it would leave the unit unable to start at all rather than correctly ordered.
Expressing that dependency would mean shipping a rule to the device to tag the
node, which is a mechanism this program does not have, and this document does
not propose one. The interface the daemon itself creates is not a candidate
either — a unit cannot wait on a device its own service brings into being.

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
    the stamp for that service has not been written yet.

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
unit name, and the paths whose contents decide whether the unit must be
restarted (§4.2). `DeviceServices` renders the recovery script from its children
rather than from a separate table, so the script's stamps cannot name a file no
resource declares.

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
    caddy=CaddyService(pin=..., acme_token=...),
    resolvers=(ResolverService(name='alice', pin=...), ResolverService(name='bob', pin=...)),
    overlay_member=OverlayService(pin=...),
    routing=RoutingSession(neighbour=..., password=...),
)
```

A fifth member is a change to this signature, not a key in a mapping that a loop
may or may not look up. What each service *is* — where it keeps state, which
device nodes it needs, which environment its image reads — is a fact about its
image, so it lives in that service's own declaration type beside the component
that renders its unit. The image pins arrive as configuration, one key per
service, through the same mechanism that pins container images and charts
(§11.1).

This retires `conventions.GW_ESTATE`, `estate.census`, and the three runtime
cross-checks that stood in for a type: a pin with no member, a member with no
pin, and a member with no address all become impossible to write. It also
retires the function the one-idiom rule was raised against — the census built
one list three ways, and a signature builds none.

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

**`Overlay` declares no policy.** The flow-rule program arrives as a parameter,
composed by a pure function in `components/overlay/flow_rules.py` that takes the
facts and returns text:

```python
def flow_rules(*, gateway: IPv4Address, homelab: IPv4Address, resolvers: Sequence[IPv4Address]) -> str: ...
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
    lifecycle. Its decisive limitation is that it implements neither `diff` nor
    `read`: the engine diffs the declared inputs against state and nothing else,
    so a file edited on the device is invisible to `preview` and to `refresh`.
    `remote.CopyToRemote`'s `triggers` only ever *replace*.
*   **A dynamic provider.** Pulumi's criteria: no existing provider covers the
    resource, the logic is specific to a single program, and it need not be
    shared across languages or teams. Limitations, all of which apply here:
    Python and TypeScript only; `read` is documented as non-functional, so
    `pulumi import` and `get` are unavailable; `refresh` and `destroy` need
    `--run-program` because the implementation lives in the program; every
    resource stores a serialized copy of the provider in state, always marked
    secret; all dynamic resources share one type token, which makes policy
    packs identify them by property; and — the one that shapes §7.4 — **a
    dynamic provider cannot be passed through `opts.provider` and does not
    inherit down a component tree.** Provider options are matched by the package
    name in the resource's type token, and a dynamic resource's package is
    `pulumi-python`, which no provider resource can be.
*   **A full provider** (bridged or native). Gains everything the dynamic one
    lacks: `import`, `read`, cross-language use, ordinary provider inheritance,
    no per-resource blob. Costs a second language and a release pipeline in a
    Python repository, for something nothing outside this repository consumes.

### 7.2 The device files: a dynamic provider

**Recommendation: keep it, deliberately.** The property that decides it is drift
detection. What makes this convergence rather than record-keeping is that `diff`
opens a session and compares the device's bytes with the declared ones, so a
file someone edited on the box appears in `pulumi preview` without a refresh. The
Command provider cannot express that at all; a full provider could, at the cost
of a Go codebase to maintain for one device.

Pulumi's three criteria are met exactly: no provider covers this, the logic is
specific to this program, and nothing else will ever consume it. The costs are
accepted with their consequences named:

*   **No `pulumi import`.** Nothing here needs it: the resources are created by
    this program, and a device that already has the files converges to the same
    content on the first apply rather than needing adoption.
*   **`read` is implemented but is not the drift mechanism.** `diff` is. A
    `refresh` that needs the provider must be run with `--run-program`, which is
    a note for the operations documentation rather than a design constraint.
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

Pulumi's own guidance is that passing credentials as resource inputs is an
antipattern — they belong to the provider, out of band. Today all five
connection properties are inputs on every file and artifact resource. They move
onto the provider instance:

```python
self.device = DeviceFileProvider(session=DeviceSession(host=..., host_key=..., private_key=...))
```

`Gateway` constructs it once and hands the same instance to each `Container` and
to the two files it declares itself. That is as close to inheritance as the
mechanism allows: a dynamic provider cannot travel through `opts.provider`
(§7.1), so it travels as an ordinary Python object down the same tree the
components already form. Pulumi serializes it into each resource's `__provider`
property and marks that secret by default, so the credential is better protected
there than as an input.

Two properties the current design relies on have to survive that move:

1.  **A rotated credential or a moved address must be a change.** Calling a
    rotation "no change" because the file on the device is already right leaves
    the superseded key in state, and a delete months later would authenticate
    with a key that no longer opens the door.

    The obvious mechanism is to compare the serialized provider, which the
    engine hands to `diff` in both property bags. It is rejected: the SDK pickles
    the provider *class by value*, so editing any line of provider code — or
    bumping the SDK, the pickling library, or Python — changes the serialization
    and would diff every resource under that provider at once. Those updates
    would rewrite bytes the device already has, which is harmless, but a wave of
    them would train a reader to skim past the one diff that matters.

    Instead, each resource declares one input the provider derives from its
    session: a readable endpoint (`user@host:port`) and a short digest over the
    two credentials. A move or a rotation changes it and is therefore a diff; a
    change to provider code does not, exactly as today. It is one small property
    instead of five, it says which session wrote the file, and it carries no
    secret.
2.  **The pinned host key must stay reviewable.** It is a public key, and a pin
    a reader can check beats a pin the engine redacts. It becomes a convention
    rather than a configuration secret (§11), which is where the homelab host's
    pinned key already lives, and `Gateway` registers it as one of its own
    component outputs so the value a deployment trusts is stated once rather
    than on a dozen resources.

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

**Every provider this program uses is explicit, constructed where its
credentials are read, and set on the top-level component that needs it. Nothing
below that ever names it again.** This is Pulumi's documented mechanism and the
legacy repository's convention: a provider set on a component resource becomes
the default for its whole subtree, transitively, because each child inherits its
parent's provider map and the first match by package name wins. So a `Gateway`
built with a controller provider gives it to `SiteFirewall`'s zone, network,
policies and port forward without any of them mentioning it.

Three consequences:

*   **`child_opts(provider=...)` disappears from component bodies.** Today
    `SiteFirewall` and `Overlay` re-plumb their provider into every child's
    options. After this, the provider arrives on the component and inheritance
    does the rest. Invoke options still take one explicitly — an invoke is not a
    resource and inherits nothing.
*   **The ambient namespaces retire.** `oci:` and `b2:` configure default
    providers today, which is configuration acting at a distance: the same
    program run with a different ambient environment declares against a
    different account. `pulumi:disable-default-providers`, listing the packages
    this program uses, turns "somebody forgot the provider" from a silent
    fallback into an error. Dynamic resources are unaffected — they carry their
    provider instance directly (§7.4) — which is why the setting lists packages
    rather than `*`.
*   **A credential that exists only to configure a provider is read where that
    provider is constructed.** It is an implementation detail of that provider
    instance, not a fact any component needs, so it stops being a constructor
    parameter threaded through components. This is a deliberate refinement of
    the layering rule, and the test still holds: no *parent* has an opinion
    about it, so it is not a parameter.

### 8.2 Every provider this stack uses

| Provider | Built by | Set on | Credential |
| --- | --- | --- | --- |
| `oci` | the stack program | the cloud, Talos-image and node-volume components | `oci:userOcid`, `oci:fingerprint`, `oci:privateKey`, read there |
| `b2` | the stack program | `BackupBucket` | the B2 key pair, read there |
| `unifi` | the stack program | `Gateway`, which passes it to `SiteFirewall` | `unifiApiKey`, read there |
| `zerotier` | the stack program | `Overlay` | `zerotierApiToken`, read there |
| `libvirt` | the stack program | `HomelabHost` | `libvirtPrivateKey`, read there (§8.3) |
| `device_files` | `Gateway` | its own children, as an object (§7.4) | `gatewayPrivateKey`, read by the stack program and passed in |

The OCI row is the one that changes most. Its region and tenancy come from
`conventions` (§10.3) and its credential from configuration, both read at the
one line that builds the provider; the components below take neither. The row
also settles a question that has been open: there is no pin-equal test between
`conventions` and the `oci:` namespace, because there is no second copy left to
disagree.

The `device_files` row is the exception that proves the rule, and §7.4 says why:
the mechanism cannot carry a dynamic provider through resource options, so the
"set on" column means an object passed down the same tree.

Because the gateway's session credential is read by the stack program while the
provider is built by `Gateway`, that one is passed in rather than read in place.
The alternative — `Gateway` reading `gatewayPrivateKey` itself — keeps the
credential nearer its provider but puts a configuration read inside a component
for a value the component otherwise has no opinion about. Both readings of the
rule are defensible; this document picks the one that keeps every configuration
read in the stack program, so a reader finds the whole configuration surface in
one file.

### 8.3 The libvirt transport's absolute paths

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
provider expands environment variables in both values and then opens them
without anchoring them anywhere, so a relative value resolves against the plugin
process's working directory — which Pulumi sets to the project directory, the
one holding `Pulumi.yaml`, and that is the same directory this repository
already calls the checkout root. Pulumi's own documentation prescribes exactly
this for exactly this symptom: a path in a resource property should be relative
to the working directory, or running the project on two machines produces
diffs.

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

The Python functions keep their names and signatures and become one line each,
so the shell quoting, the ordering guarantees and the rendered text all stay
under test exactly as they are. The one verbatim file in the table is what the
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
class ContainerService:
    # One service the device runs: what it is called, where it sits, what it serves.
    name: str
    address: IPv4Address | None         # None: the host's own network namespace
    vhost: str | None = None
```

The last field carries a comment pointing at dns.md §4, for why a name the
gateway serves is a name in the public zone that public resolvers do not answer
for.

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

The OCI region and tenancy are claimed twice today — `conventions.OCI_REGION`
and the `oci:` configuration namespace — and `b2Region` sits in stack
configuration with an argument ("an account property, permanent") that is the
argument for `conventions`.

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

The `oci:` and `b2:` namespaces retire entirely — not "retire as readers", but
stop existing: with default providers disabled there is nothing left to
configure through them. The identity-domain endpoint and name that used to sit
beside these are gone too, for a different reason: they were read only to
declare the chunk store's user, and the chunk store is deleted (§10.5). The
credentials scripts keep their own copy of those two facts, which is where they
belong — they are how a mint talks to the domain, not how this stack declares
anything.

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
| `oci:userOcid`, `oci:fingerprint`, `oci:privateKey` | the line that builds the OCI provider |
| the B2 key pair | the line that builds the B2 provider |
| `unifiApiKey` | the line that builds the controller provider |
| `zerotierApiToken` | the line that builds the overlay provider |
| `libvirtPrivateKey` | the line that builds the libvirt provider (§8.3) |
| `gatewayPrivateKey` | the line that builds the device-file provider |
| `gatewayBootstrapHost` | the same line — it is where that provider dials |
| `gatewayRootfs` | four `image:` pins (§11.1) |
| `oci:region`, `oci:tenancyOcid` | `conventions.providers` (§10.3) |
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

### 11.1 Image pins share the versions mechanism

The four root filesystems are pinned exactly the way container images and Helm
charts are pinned, because that is what they are: a build someone else produced,
selected by version. The repository already has that mechanism — a `versions`
object over the `image:` and `chart:` configuration namespaces, where
`versions.chart['cert-manager']` reads `chart:cert-manager` and splits a
`repository:version` pair. The root filesystems join it as `image:` entries, one
key per service:

```yaml
image:gateway-caddy: rootfs-1:e154a141364c60cc…
image:gateway-adguard-alice: rootfs-1:051ab35069306138…
image:gateway-adguard-bob: rootfs-1:051ab35069306138…
image:gateway-overlay: rootfs-1:ba74ae49ae729e79…
```

Three properties, and each replaces something the current shape gets wrong:

*   **The configuration value is a string, not a structure.** Today it is a JSON
    object of objects, parsed by hand. A version pin is a scalar in every other
    place this repository pins one, and the parse function goes away with the
    structure.
*   **The URL is not configuration.** Which repository publishes the artifacts,
    and how a release names them, is a decision — so it is a `conventions` rule
    the component applies to the release name, not four URLs an operator
    maintains. What remains configured is the release and the digest, which are
    the two things a build produced.
*   **Four keys, not one.** The fact that there are four services lives in the
    component signature (§5.3); the configuration is four independent pins. The
    two resolvers normally carry the same value, and two keys is exactly what
    lets one of them move first — a rollout that proves a new resolver image on
    one instance before the other is a thing this shape permits and a single
    shared pin forbids.

The digest is the half renovate cannot compute. A custom manager can bump the
release, but the matching digest comes from the build, so these pins stay
manual — the same status the repository already gives a pin whose upstream is
not a version stream, and stated in the renovate configuration for the same
reason. If those images ever gain proper registry tags, they become ordinary
`image:` pins and renovate takes them over with no change here.

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

Today every input the libvirt provider accepts for a domain is ignored, so
Pulumi owns one fact: that a domain with this UUID exists. That fails the
question adoption has to answer — **if somebody deletes the domain outside this
program, can an apply put it back?** With everything ignored, no: the next apply
would define a domain with no disks, no firmware, no network and no console,
which is not the home's automation, it is an empty machine wearing its name. And
if it cannot be recreated, adoption is buying nothing at all.

So the end state declares enough to **re-define the domain around the disk
images that survived it**:

| Declared | Why it must be |
| --- | --- |
| `name`, `description` | What `virsh` lists; the domain's label |
| `vcpu`, `memory` | Its sizing |
| `machine`, `arch`, `type`, `cpu` | A UEFI guest does not boot on a machine type it was not installed for |
| `firmware`, `nvram` | The variable store holds its boot entries; a recreation without it does not boot |
| `disks` | By host path. These *are* the home's automation |
| `networkInterfaces` | Bridge **and MAC**: a new MAC is a new identity to every lease and every peer |
| `consoles`, `graphics`, `video` | How a machine that fails before its own logging says why |
| `autostart`, `running` | It comes back after a host reboot |

**Ignored, and each for a reason that is not "we did not get to it":**

*   `cloudinit`, `coreosIgnition`, `kernel`, `initrd`, `cmdlines`, `fwCfgName`,
    `bootDevices`, `filesystems`, `tpm`, `qemuAgent` — mechanisms this domain
    does not use. A declaration of "none" and an absence are the same thing
    here, and the shorter one is the absence.
*   `emulator` — the host's path to its QEMU binary, which moves when the host
    updates its packages. Pinning it would make a host upgrade a diff.
*   `metadata`, `xml` — the provider's own bookkeeping and its escape hatch;
    neither describes the machine.

**What recreation restores, stated honestly: the definition, not the data.** An
apply after an accidental `virsh undefine` gets back a domain that boots the
same disk images with the same identity on the same bridge. It does not restore
the contents of those disks — if the disk images are gone too, what brings the
home's automation back is its own backup regime, and no amount of declaring in
this program changes that. The value of the declaration is precisely the part
that *is* reproducible: the definition nobody wrote down anywhere else.

Two facts about the provider shape all of this, and they are why the section is
not simply "declare everything":

*   **Almost every field replaces the domain**, `name` and `description`
    included; only `autostart` and `running` update in place. Combined with
    `protect=True`, that means a drift in any declared field is a *refusal until
    someone reconciles it*, never a silent replacement of a machine that must
    not be recreated. That is the intended behavior, and it is what "owned"
    buys: the drift is reported rather than ignored.
*   **The declaration has to start from the live domain.** The slice that lands
    this reads the domain's current definition off the host and writes those
    values down. Declaring anything else would propose a replacement on the
    first apply, which `protect` would refuse for as long as the declaration
    stood.

The ratchet that exists today — every input the provider accepts is accounted
for, so a provider release that grows a field fails a test instead of silently
proposing a change to a running Home Assistant — is kept, with its assertion
widened from "every field is ignored" to "every field is either declared or
ignored".

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
    member, a member with no pin, a member with no address (§5.3). What replaces
    them is the version mechanism's own missing-key error.
*   **The roster gains an invariants test** — unique names, unique node ids,
    unique addresses inside the overlay subnet, the gateway at the address
    clients dial, the roster within the multicast limit — which is what is left
    of `parse_members`.
*   **The adopted domain's ratchet widens.** Today one test asserts that every
    input the libvirt provider accepts is ignored; it becomes an assertion that
    every input is either declared or ignored, which is what makes §13 checkable
    rather than aspirational.
*   **Several assertions on stack configuration keys retire** with the keys
    (§11), and the site-fact refusal tests shrink to the keys that remain.

--------------------------------------------------------------------------------

## 15. How we get there

Nine slices, in this order, each an issue cut from this document. The order is
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
4.  **Providers, explicitly** (§8): every provider built where its credentials
    are read and set on its top-level component, default providers disabled, the
    ambient namespaces gone, and the libvirt URI's paths made relative. This one
    is worth its own live drill: it changes how every resource in the stack is
    authenticated.
5.  **Rendered configuration from files** (§9): the mechanism, then the eight
    literals, with the render functions' signatures unchanged.
6.  **The gateway component tree** (§4, §5): `Gateway`, `DeviceServices`,
    `Container`, the typed declarations, the units' own dependencies, and the
    vocabulary of §3 applied throughout.
7.  **The overlay leaves the gateway** (§6): `components/overlay/`, the rule
    composition as a pure function, the stack program composing it.
8.  **The device-file provider** (§7.4): the connection onto the provider
    instance, the derived session input, the pin as a component output.
9.  **The stack program** (§11, §12), then **the adopted domain** (§13) and the
    caddy certificate (§9.3) — the last two touch live systems and are worth
    landing alone.

Slices 1 to 3 are mechanical and reviewable by diff; 4 to 9 each rewrite their
own tests. None of them is a migration: the `physical` stack has no state, so
every rename in this document is a rename and not a replacement.
