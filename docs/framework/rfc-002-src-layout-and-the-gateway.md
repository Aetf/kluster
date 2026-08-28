# RFC 002: Source Layout, and the Gateway as a Component Tree

*   **Status:** Proposed. It is approved before implementation starts, and the
    implementation issues are cut from the accepted text.
*   **Created:** 2026-08-28
*   **Authority:** `docs/style/` is the canon this RFC obeys; where the canon
    is silent, a rule proposed here is marked **new rule**.
*   **Companion:** rfc-001 is the async-input framework and stays untouched.
*   **In scope:** the tree under `src/`, the gateway's component hierarchy, the
    device-file provider, the rendered-configuration mechanism, the vocabulary,
    the `conventions` restructure, and the `physical` stack's configuration
    reading, API shape and adopted domain.
*   **Out of scope, each settled elsewhere:** the `dns` and `github` stacks'
    internal reorganization — they move into the new tree here and are
    otherwise untouched, and get a document of their own; the scripts' internal
    shape; and the shared test machinery.
*   **One section is conditional:** §7.5, on per-node capabilities, depends on a
    pending decision about whether the object-storage chunk bucket exists at
    all. Everything else here holds either way; §7.5 is written to the expected
    outcome and is not final until that decision lands.

--------------------------------------------------------------------------------

## 1. Context & Problem Statement

Everything the design names is implemented, and the implementation works. What
it lacks is a shape: five kinds of code share three directories, the gateway is
a set of module functions rather than the component the design calls it, another
program's configuration languages are Python string literals, and the data
every stack agrees on is a flat namespace in which values that are only correct
together are declared apart.

None of that is a defect a test can catch, which is why it is settled by a
document before it is settled by a diff. Two facts make now the moment:

1.  **Nothing here has been applied.** The `physical` stack has no state, so
    every rename in this document is free. After the first apply the same
    renames are replacements of live resources.
2.  **The apps layer has not started.** Whatever shape the gateway takes is the
    shape thirty application components will copy.

The document states the end state. Section 12 lists the order it is reached in;
nothing else here describes a transition.

--------------------------------------------------------------------------------

## 2. Source layout

### 2.1 Five kinds of code, five homes

| Kind | Home | What it is |
| --- | --- | --- |
| Stack programs | `kluster/stacks/` | Wiring. Reads configuration, builds top-level components, exports outputs, declares no resource of its own. |
| Components | `kluster/components/<area>/` | Every reusable unit of resources, down to leaf resources. |
| Custom providers | `kluster/providers/<name>/` | Dynamic providers: the code that talks to a system Pulumi has no provider for. |
| Scripts | `kluster/scripts/<name>/` | Console entry points declared in `pyproject.toml`. |
| Shared helpers | `kluster/lib/`, `putils` | Code with no resources and no domain. |

The two helper homes are not the same thing. `putils` is the Pulumi framework
of rfc-001 and knows nothing about this estate; `kluster/lib/` is estate-generic
but needs no Pulumi — configuration reading, template loading, workstation
slots, the Kubernetes helpers, the chart pins.

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
    components/
      gateway/                # was kluster/gateway/
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
    else.
2.  **`kluster.providers` does not import `kluster.conventions`.** A provider is
    generic code for a class of system; the estate's decisions are its callers'.
    (**New rule.** The canon says custom providers live apart from the
    declaration logic that uses them; this states what "apart" forbids.)
3.  **`putils` imports no `kluster` package at all.**
4.  **Nothing imports `kluster.stacks`** except `kluster.main`, which dispatches
    them.

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

The canon's naming rules produce a glossary, and the glossary is the contract
the code follows. It is kept in the `conventions` package's own module
documentation, where the style reviewer reads it.

### 3.1 Renames

| Today | Becomes | Why |
| --- | --- | --- |
| `Estate`, `estate.py`, "the estate" | `DeviceServices`, `services.py` | "Estate" means four unrelated things across the docs: this container set, the site's address plan, the DNS records no app owns, and the operator's own credential world. The canon's own example retires it. |
| `Seed`, `adguard_seed`, `seed_state` | `InitialState`, `initial_state` | "Seed" stays reserved for the two places it is the target system's own word: the `nocloud` seed image, and the credential seed kit. |
| `Dropin`, `Container.files` | `MountedFile`, `Container.mounted_files` | These are not systemd drop-ins. They are files written on the device and bind-mounted into the container read-only, and the name now says so — which frees "drop-in" for its real meaning. |
| `GwFile`, `GwArtifact`, `GW_*` | `DeviceFile`, `DeviceArtifact`, `gateway.*` | "gw-config" is the name of a convention the device supports, not a prefix every symbol needs. |
| `zerotier.Network` | `Overlay` | It collides with `unifi.Network`, which is a LAN. |
| `ZtMember`, `ZT_ROSTER` | `OverlayMember`, `overlay.ROSTER` | One term per concept, and the package path already says ZeroTier. |
| `Enrolled` | *(deleted)* | Its two fields become roster fields (§7.2). |
| `Firewall` (UniFi) | `SiteFirewall` | Distinguishes it from the overlay's flow rules, which are also a filtering policy. |
| `on_boot_script`, `20-kluster-estate.sh` | `recovery_script`, `20-kluster-services.sh` | Named for what it does rather than for the directory it sits in. |
| `parse_rootfs`, `parse_addresses`, `parse_members` | `lib.config` readers, or deleted | §7.2 and §8. |
| `facts.py` | *(deleted)* | §7.4. |
| `AUGMENTED_NODE`, "the augmented node" | `DEDICATED_VIP_NODE`, `NODE_VOLUMES` | One name for two unrelated capabilities that happen to sit on one machine today (§7.5). |
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

The device itself keeps the four names the design uses, each for its own role,
because they are not synonyms: **the gateway** (the role, and the component),
**the device** (the target of a provider), **the box** (its userland and
hardware), **the UDM** (the appliance in networking context).

"Estate" survives in one meaning only — the DNS records that belong to no
application, which is what the `dns` package already calls them. It never again
names the container services, the address plan, or the operator's credentials.

--------------------------------------------------------------------------------

## 4. The gateway as a component tree

### 4.1 The tree

```text
Gateway                       components/gateway/__init__.py
├── DeviceServices            components/gateway/services.py
│   ├── recovery script       one DeviceFile: what every other file's hook runs
│   ├── routing config        one DeviceFile: the daemon's own, applied by itself
│   └── Container × 4         components/gateway/container.py
├── SiteFirewall              components/gateway/unifi.py
└── Overlay                   components/gateway/overlay.py
```

The three named in title case are components; the two in lower case are single
resources, which stay resources — a component wrapping one file would be
ceremony, and the canon's rule is that a *set* of resources with a name in the
design is a component.

`Gateway` is the component the design names, and it is what the stack program
constructs. It owns the three doors the device is configured through — a shell,
the controller's API, the overlay's API — and therefore owns the three
credentials, one per child. It declares no resource of its own beyond its
children, and it is where the device-file provider is constructed (§5).

`declare_estate`, `declare_firewall` and `declare_zerotier` disappear with it:
they are a second name for each child and hide the component from the reader
(§9).

### 4.2 `Container` is a component

Each container service is a `Container` component owning everything that
belongs to it and nothing that belongs to a sibling:

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
restarted. `DeviceServices` renders the recovery script from its children rather
than from a separate census, so the script's restart stamps cannot name a file
no resource declares.

**The census is typed parameters, not a mapping.** `DeviceServices` takes the
four services by name and shape:

```python
DeviceServices(
    name,
    provider=...,
    caddy=CaddyService(pin=..., acme_token=...),
    resolvers=(ResolverService(name='alice', pin=...), ResolverService(name='bob', pin=...)),
    overlay_member=OverlayService(pin=...),
    routing=RoutingSession(neighbour=..., password=...),
)
```

A fifth member is a change to this signature, not a key in a mapping that a
loop may or may not look up. What each service *is* — where it keeps state,
which device nodes it needs, which environment its image reads — is a fact about
its image and lives in its own `dataclass`, beside the component that renders
its unit. What is cross-stack — the service names, their container-VLAN
addresses, their vhosts, the resolver API port — is `conventions` (§7.1). The
image pins arrive as configuration, validated by name at the stack boundary
(§8), because a digest is whatever the build produced.

This retires `conventions.GW_ESTATE`, `estate.census`, and the three runtime
cross-checks that stood in for a type: a pin with no member, a member with no
pin, and a member with no address all become impossible to write. It also
retires the function the canon's one-idiom rule was raised against — the census
built one list three ways, and a signature builds none.

### 4.3 Ordering belongs to systemd

The units state their own requirements, and the recovery script stops choosing a
start order:

*   every service keeps `Wants=`/`After=network-online.target`;
*   a service on the container VLAN adds `BindsTo=` and `After=` the bridge's
    device unit, so a container cannot be started against a bridge that does not
    exist yet — today that race is absorbed by `Restart=always`, which is a
    retry loop standing in for a dependency;
*   the overlay member adds the same pair for the tunnel device it opens.

There are **no mutual dependencies between the four services**, and the units
say so by declaring none. The caddy instance proxies to the resolvers at request
time, not at start time; the overlay member carries the management session, not
the other containers' traffic.

The one ordering that remains in the recovery script is the restart of the
service carrying the session the apply arrived on, which is done last. That is a
property of the transport, not of the system: expressing it as a unit ordering
would state something false about how the device boots. The script says so where
it does it.

### 4.4 `SiteFirewall`

Unchanged in substance: the cluster VLAN as a network object, its own firewall
zone, the policy census, the port forward, the static host entries. Three
changes follow from elsewhere in this document:

*   the IoT VLAN's unique-local prefix stops being a literal declared beside the
    rules and comes from the site network it belongs to (§7.1), which is the
    fifth place that prefix is currently spelled;
*   the cluster subnet string is derived from the same structure rather than
    assembled from two constants at module scope;
*   the peer port is a convention rather than a configuration key (§8).

### 4.5 `Overlay`, and where the flow rules are composed

`Overlay` declares the network, its managed routes, the generated identities and
the roster's members. It declares **no policy**: the flow-rule program arrives
as a parameter.

The composition lives in `components/gateway/flow_rules.py`, beside the
`Gateway` that has the facts. The reason is that the confinement is not a fact
about ZeroTier: it is a fact about how continuous integration reaches this
site, and it names two destinations the overlay knows nothing about — the
homelab host's libvirt session, and the resolvers.

The resolvers are the interesting half, and the comment at the composition site
says it plainly rather than by implication:

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

## 5. The device-file provider

### 5.1 Connection state on the provider

`DeviceFile` and `DeviceArtifact` stop carrying `host`, `port`, `username`,
`private_key` and `host_key` as declared inputs. The connection is constructed
once, by the component that owns the session — `Gateway` — and handed to the
provider instance every resource under it is created with:

```python
self.device = DeviceFileProvider(session=DeviceSession(host=..., host_key=..., private_key=...))
```

A dynamic provider is not a `ProviderResource`, so this is a constructor
parameter rather than `opts.provider`; the effect the canon asks for is the
same, and it is the only form the mechanism has. Pulumi serializes the provider
object into each resource's reserved `__provider` property, resolving any
`Output` it captured on the way, and marks that property secret — so a session
credential is as protected there as it is today, and better hidden.

### 5.2 What that costs, and what pays for it

Two properties the current design relies on have to survive the move, and each
needs a mechanism:

1.  **A rotated credential or a moved address must be a change.** Calling a
    rotation "no change" because the file on the device is already right leaves
    the superseded key in state, and a delete months later would authenticate
    with a key that no longer opens the door. The provider's `diff` therefore
    compares the serialized provider between old and new alongside the declared
    inputs. This is sound because the SDK pickles providers deterministically —
    it sorts dictionary items precisely so that an unchanged provider serializes
    to an unchanged string — and because the diff already receives both
    property bags. A serialized provider that is still unknown during a preview
    is handled by the same unknown check the inputs already go through.
2.  **The pinned host key must stay reviewable.** It is a public key, and a pin
    a reader can check beats a pin the engine redacts; today it is a plain input
    for that reason, and inside the provider it would be part of an opaque
    secret. So `Gateway` registers the endpoint and the pinned key as its own
    component outputs. The fact is then stated once, at the component that owns
    the session, instead of on each of a dozen resources — which is a better
    answer to "what does this deployment trust" than the current repetition.

The resource identifier stops being built from the property bag and is built
from the session the provider holds, which is where that information now is.

### 5.3 The providers' package

`providers/device_files/` holds the two resources, their providers, the
exceptions they raise and the SSH transport. `providers/talos_factory/` holds
the image-factory resource that is today mixed into `physical/image.py`; the
`TalosImage` and `TalosNocloudImage` components that use it stay components, in
`components/talos/`. Neither package imports `conventions` (§2.3), which is
already true of both today and is what makes them providers rather than
declarations.

--------------------------------------------------------------------------------

## 6. Rendered configuration comes from files

### 6.1 The mechanism

One mechanism for the repository, in `lib/templates.py`:

```python
def load(package: str, name: str) -> str: ...
def render(package: str, name: str, params: object) -> str: ...
```

`load` returns a static file beside the module. `render` renders a Jinja2
template with a frozen `dataclass` of parameters, which is what makes the
template's inputs typed at every call site. Both find the file through
`importlib.resources`, so a template is located relative to its package and
works from a checkout and from an installed wheel alike.

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

### 6.2 What moves

| Literal today | Becomes |
| --- | --- |
| `estate.frr_config` | `gateway/templates/frr.conf.j2` |
| `estate.unit_file` | `gateway/templates/container.service.j2` |
| `estate.on_boot_script` | `gateway/templates/recover-services.sh.j2` |
| `estate.caddyfile` | `gateway/templates/Caddyfile.j2` |
| `estate.adguard_seed` | `gateway/templates/adguard-home.initial.yaml.j2` |
| `zerotier.flow_rules` | `gateway/templates/flow-rules.zt.j2` |
| `homelab.disk_tuning_xslt` | `homelab/templates/disk-tuning.xslt` (static) |
| `image._schematic_document` | `talos/templates/schematic.yaml.j2` |

The Python functions keep their names and signatures and become one line each,
so the shell quoting, the ordering guarantees and the rendered text all stay
under test exactly as they are.

### 6.3 The gateway's own certificate

The caddy configuration is the one template whose content changes, and the
reason is a decision recorded on 2026-08-28. The gateway's three vhosts are
names in the primary zone that resolve nowhere publicly (dns.md §4), and the
gateway issues their certificate itself so that its TLS keeps renewing while the
cluster is down. Two consequences the template has to carry:

*   **One wildcard certificate, not three per-name ones.** Every issued
    certificate is published in Certificate Transparency logs, and per-name
    issuance would republish exactly the census that resolving nowhere was meant
    to hide.
*   **A SAN set that differs from the cluster issuer's.** cert-manager holds a
    per-zone wildcard too. Let's Encrypt counts its duplicate-certificate limit
    by identifier set across accounts, so two issuers asking for exactly
    `*.<zone>` share one weekly window and a crash-looping renewal on either
    side can lock the other out. The gateway's block therefore names the apex
    beside the wildcard, which puts it in a bucket of its own.

So the file becomes one site block for `*.<zone>` and `<zone>`, with the three
vhosts matched inside it and everything else refused. The exact directive
spelling is verified against the caddy build the device runs, in the slice that
lands it.

--------------------------------------------------------------------------------

## 7. The `conventions` restructure

### 7.1 Shape

`conventions.py` becomes `kluster/conventions/`, one module per domain, and its
constants become structures — the canon's illegal-states rule applied to data:
values that are only correct together are declared together, so using one
without its siblings does not parse.

| Module | Holds |
| --- | --- |
| `identity.py` | The cluster name, the stack and appliance names, the label domain. |
| `site.py` | One `SiteNetwork` per home network, one `AddressPool` for the `lan` pool, the site's unique-local prefix. |
| `overlay.py` | The overlay subnet, the roles, the managed routes, `ROSTER`. |
| `gateway.py` | The device's paths, account and pinned host key, the service census, the vhosts, the resolver API port. |
| `homelab.py` | The worker node's name, address and sizing, the host bridge, the host's pinned key, the adopted domain's UUID. |
| `cloud.py` | The node fleet, node sizing, the VCN plan, the per-node capabilities of §7.5. |
| `cluster.py` | Pod and service ranges, BGP, the load-balancer pools, the Gateway API names, storage classes. |
| `backup.py` | Retention classes, bucket names, repository layouts. |
| `dns.py` | Zones, mirrors, anchors. |
| `providers.py` | The provider account facts (§7.3). |

The structures that matter most:

```python
@dataclass(frozen=True)
class SiteNetwork:
    """One subnet the gateway serves, in both families, with its own leg on it."""
    name: str
    v4: IPv4Network
    v6: IPv6Network
    vlan_id: int | None = None          # None: the untagged LAN
    gateway_v4: IPv4Address | None = None
```

A site network's IPv6 prefix is numbered after the third octet of its IPv4
subnet — that is the site's addressing rule — so declaring the two apart is
declaring half a decision. Today the IoT VLAN's prefix is written out by hand in
`gateway/unifi.py`, four hundred lines from its IPv4 sibling.

The same treatment gives the `lan` pool its address groups and its fixed VIPs in
one object, and the gateway its service census:

```python
@dataclass(frozen=True)
class ContainerService:
    """One service the device runs: what it is called, where it sits, what it serves."""
    name: str
    address: IPv4Address | None         # None: the host's own network namespace
    vhost: str | None = None
```

The last field carries a comment pointing at dns.md §4, for why a name the
gateway serves is a name in the public zone that public resolvers do not answer
for.

### 7.2 The overlay roster carries identities

**Operator ruling, 2026-08-28:** a node id is minted by the device and
never changes — an identity, not configuration — and a roster entry must match
one concrete instance on the network. So the roster carries the node id *and*
the address for every member, and four things follow:

1.  **`zerotierMembers` configuration retires.** Its ten entries move into
    `ROSTER` as code.
2.  **`parse_members` becomes roster validation.** There is no longer an
    untyped mapping to cross-check against the roster; what remains is the
    roster's own invariants — names unique, node ids unique, addresses unique
    and inside the overlay subnet, the gateway's entry at the address every
    client dials, the roster no larger than what multicast reaches. It runs as a
    test, and at the component's boundary.
3.  **The `udm` entry is added at ceremony step 2, as a commit.** The gateway is
    the one member whose identity this program's own work creates: the daemon is
    a container service on the device, and its node id does not exist until the
    first delivery has run. Step 2 of the bring-up ceremony (gateway.md §2.5) reads
    that id off the device; recording it is an edit to the roster rather than to
    stack configuration. Until then the entry is absent, and absent is a state
    the code can read: no member is declared for the gateway, and no `udm.zt`
    record is published. `gatewayBootstrapHost` therefore loses one of its two
    jobs and keeps the other — it is now only the answer to "where does the
    device answer today".
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

### 7.3 Provider account facts

There is an open question about where the OCI region and tenancy live, since
`conventions.OCI_REGION` and the `oci:` configuration namespace both claim them,
and `b2Region` sits in stack configuration with an argument — "an account
property, permanent" — that is the argument for `conventions`.

**Proposal: `conventions/providers.py`, one structure per account.**

```python
@dataclass(frozen=True)
class OciTenancy:
    region: str
    tenancy_ocid: str
    identity_domain_url: str
    identity_domain_name: str
    user_email_domain: str
    compartments: Mapping[str, Compartment]

@dataclass(frozen=True)
class B2Account:
    region: str
```

Every reader in this repository reads these from there. Two keys stay in stack
configuration and only one of them is a copy:

*   `oci:region` and `oci:tenancyOcid` remain in the `oci:` namespace, because
    that namespace is the provider SDK's own contract and the ambient
    configuration is how the OCI provider is configured. A test pins them equal
    to `conventions`, so the two homes cannot silently disagree, and the mint
    that writes the credential **verifies** the tenancy it authenticated against
    instead of writing its own copy of the fact.
*   `oci:userOcid`, `oci:fingerprint` and `oci:privateKey` are the credential
    and rotate with a re-mint. They stay secrets.

The alternative — an explicit `oci.Provider` built from `conventions` plus the
credential, and no ambient namespace at all — is the more orthodox reading of
the canon's provider rule, and it is rejected here: it threads one provider
through every OCI component *and* every invoke in the stack, and buys nothing in
a program that has exactly one tenancy. `b2Region` has no ambient consumer at
all and moves outright.

### 7.4 `facts.py` folds into configuration reading

`gateway/facts.py` is two functions that turn `require_object`'s untyped result
into typed values with a named error. That is not a gateway concept: it is how
this repository reads configuration, so it becomes `lib/config.py` and is used
by every stack that reads an object. The validation stays at the boundary and
still reports which entry is wrong by name; only the home changes, and the
gateway stops owning a general mechanism.

### 7.5 Per-node capabilities

**Status: conditional.** This subsection depends on a decision, pending as this
is written, about whether the object-storage chunk bucket and the JuiceFS mount
in front of it exist at all. The expected outcome is that they do not: the one
dataset behind them moves to a plain block volume on a cloud node, the
`ChunkStore` component is deleted rather than moved, and the credential it mints
goes with it. Nothing else in this document depends on that; this subsection
does, and is not final until the decision lands.

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
class NodeVolume:
    # A block volume attached to one node, and where that node mounts it.
    node: str
    size_gb: int
    mount: str

NODE_VOLUMES: Mapping[str, NodeVolume] = {...}
DEDICATED_VIP_NODE = ...
```

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

**One volume per node, spread.** The dataset that needs the VIP keeps its volume
on the VIP node; a second volume goes on a different node. Three reasons, in
order of weight: disk selection in the machine configuration stays "the disk
that is not the boot disk" rather than a discrimination by size or serial;
losing one node stops taking two preserved datasets with it; and the load
spreads. The cost is identical either way, which is what makes the placement
free to choose on these grounds.

The word "augmented" retires with the bundle. The `augmented` parameter of the
cloud-nodes component becomes the two capabilities, passed separately, and the
node that holds each is read from `conventions` rather than handed down as one
name.

--------------------------------------------------------------------------------

## 8. What the `physical` stack reads

The canon's rule is that configuration is read at the layer owning the concept,
and that `conventions` holds decisions and identities while configuration holds
what an operator supplies or rotates. Applied to every read the stack performs
today:

| Key | Read today | After |
| --- | --- | --- |
| `talosVersion` | stack | stack, passed to the Talos components |
| `libvirtPrivateKey` | stack | stack — a credential |
| `libvirtStorageDir` | stack | stack — a path on someone else's host |
| `gatewayPrivateKey` | stack | stack — a credential |
| `gatewayBgpPassword` | stack | stack — a credential |
| `gatewayAcmeToken` | stack | stack — a credential |
| `unifiApiKey` | stack | stack — a credential |
| `zerotierApiToken` | stack | stack — a credential |
| `budgetAlertRecipients` | stack | stack — operator-supplied, rotates |
| `gatewayRootfs` | stack | stack — build outputs, validated by name |
| `gatewayBootstrapHost` | stack | stack — a ceremony knob, absent in steady state |
| `workerGua` | stack | stack — observed off a booted machine |
| `oci:tenancyOcid` | stack | `conventions.providers`, pinned equal (§7.3) |
| `ociIdentityDomainUrl` | stack | `conventions.providers` |
| `ociIdentityDomainName` | stack | `conventions.providers` |
| `b2Region` | stack | `conventions.providers` |
| `zerotierMembers` | stack | `conventions.overlay.ROSTER` (§7.2) |
| `zerotierNetworkId` | stack | `conventions.overlay` — the identity of the adopted network |
| `gatewayAddresses` | stack | `conventions.gateway` — see below |
| `gatewayHostKey` | stack | `conventions.gateway` — see below |
| `haosDomainUuid` | stack | `conventions.homelab` — see below |
| `qbittorrentPeerPort` | stack | `conventions` — see below |

Five of those are judgment calls rather than applications of an existing ruling,
and each has a precedent in the repository:

*   **The container-VLAN addresses.** Nothing outside this program assigns them:
    the program injects each address into its own container's environment, and
    three declarations must agree on it — the caddy site that proxies the
    instance, the initial state that tells it where to listen, and the flow rule
    that admits a run to it. That is the same argument that already put the
    resolver API port in `conventions`.
*   **The gateway's pinned host key.** The homelab host's pinned key is already
    a constant in code, with the reason written beside it: a public key is not a
    secret, and a pin typed in beside the client credential could be replaced by
    whoever could already replace the credential. The gateway's key is the same
    kind of fact and is presently a configuration secret, which also redacts it
    from the previews where a reader would check it.
*   **The adopted domain's UUID.** It is minted by libvirt on the host and never
    changes — the same shape as a node id, under the same ruling.
*   **The peer port.** `conventions` already carries a public-port census; the
    peer port is named twice in the firewall — the pinhole and the forward — and
    belongs beside it.
*   **The overlay network's id.** It is what the network is adopted by: minted
    by the service, stable, and meaningless to change. It is not a secret
    either; joining the network takes an authorized member, not knowledge of its
    id.

What is left in stack configuration after this is credentials, values that
rotate, values observed off running machines, and the one ceremony knob — which
is the definition the canon gives.

Two further notes, both facts rather than proposals. `ociIdentityDomainUrl` and
`ociIdentityDomainName` are required by the stack today and are set in no
committed configuration, so the storage arm cannot yet run; folding them into
`conventions` is also what fixes that. And the `oci:` namespace read moves out
of the stack program's own constants (`OCI_NAMESPACE`, `OCI_TENANCY_KEY`), which
disappear with it.

--------------------------------------------------------------------------------

## 9. One shape per role

The stack program declares six domains today in four different shapes: a
component constructed directly, a module function returning a component, a
module function returning nothing, and a stack-private helper. The canon asks
for one.

**A domain is a component class, and the stack program constructs it.** No
`declare_*` wrapper exists anywhere: a wrapper is a second name for the same
thing, it hides the component from the reader, and in one case it throws the
component away by returning `None`.

The stack program keeps private helpers only where a domain needs several
configuration reads to become one constructor call, and each such helper is
named for its domain and returns the component. **Exports move into one block**
in the entry point — today they are scattered across four functions, and they are this
stack's whole contract with `dns`, `k8s-base` and the credential machinery, so
they are worth reading in one place.

--------------------------------------------------------------------------------

## 10. The adopted HAOS domain

Adoption is step one; the end state is a declaration with an owner. Today every
input the libvirt provider accepts for a domain is ignored, which means Pulumi
owns only the fact that the domain exists.

**Declared and owned:** `name`, `description`, `autostart`, `running`, `vcpu`,
`memory`.

Those are the facts an operator would state about this VM, and the first four
are updatable in place — "the home's automation comes back after a host reboot"
is exactly the kind of promise a declaration should hold. `vcpu` and `memory`
replace the domain rather than updating it, and the domain is protected, so a
drift there becomes a refusal until someone reconciles it; that is the intended
behavior for a machine nothing may recreate. The slice that lands this reads the
live values off the host and declares those, because declaring anything else
would propose a replacement on the first apply.

**Still ignored, in three groups, each with its reason:**

*   **Identity-bearing** — `disks`, `nvram`, `filesystems`, `tpm`. These *are*
    the domain; the XML is only metadata about them.
*   **Host-minted values that would diff forever** — `networkInterfaces` (the
    MAC address is the host's), `consoles`, `graphics`, `video`, `emulator`,
    `machine`, `arch`, `type`, `firmware`, `cpu`, `metadata`, `xml`.
*   **Not applicable to this domain** — `cloudinit`, `coreosIgnition`, `kernel`,
    `initrd`, `cmdlines`, `fwCfgName`, `bootDevices`, `qemuAgent`.

The ratchet that exists today — every input the provider accepts is accounted
for, so a provider that grows a field fails a test instead of silently proposing
a change — is kept, with its assertion widened from "every field is ignored" to
"every field is either declared or ignored".

--------------------------------------------------------------------------------

## 11. What the tests look like after

The shared test machinery is settled elsewhere; this section says only what the
work described here leaves for it.

*   **The gateway's suite splits by component**: the services and their
    containers, the overlay, the site firewall, the flow-rule composition, and
    the device-file provider each get a file, replacing one file that covers
    four subjects.
*   **The rendered-text cases survive unchanged.** The render functions keep
    their names and signatures across the move to templates (§6.2), so the
    cases that assert on fragments of a unit file, a routing
    configuration, a caddy configuration or the recovery script keep asserting on
    what they assert on now. That is the point of keeping the functions.
*   **Three census tests become type-level and disappear**: a pin with no
    member, a member with no pin, a member with no address (§4.2). What replaces
    them is one boundary test that a configuration object missing a service's
    pin is refused by name.
*   **The connection tests change shape.** The case asserting that a connection
    hands every resource the same five properties is deleted with the design it
    describes; the two cases proving that a moved address and a rotated
    credential are changes are rewritten against the serialized provider, which
    is what §5.2 makes them a diff of.
*   **The roster gains an invariants test** — unique names, unique node ids,
    unique addresses inside the overlay subnet, the gateway at the address
    clients dial, the roster within the multicast limit — which is what is left
    of `parse_members`.
*   **Four assertions on stack configuration keys retire** with the keys
    (§8), and the site-fact refusal test shrinks to the keys that remain.

--------------------------------------------------------------------------------

## 12. How we get there

Eight slices, in this order, each an issue cut from this document. The order is
chosen so that nothing is moved twice: homes first, then the data every layer
reads, then the text, then the structure that consumes both.

1.  **The layout move.** The tree of §2.2, as renames with no behavior change,
    plus the `lib/workstation.py` split and the import contract of §2.3 in CI.
    The `dns` and `github` packages move with it and are otherwise untouched.
2.  **`conventions` restructured** (§7.1, §7.3): the package, the structures,
    the provider account facts. Readers follow; no vocabulary changes yet. The
    per-node capabilities of §7.5 land here too, once the decision they wait on
    has been made — they are a structure like the others, and splitting them
    into a slice of their own would restructure the same file twice.
3.  **The roster carries identities** (§7.2): `zerotierMembers`, the
    `zerotier_addresses` export and the `dns` StackReference retire together,
    because each is the other's only remaining reason to exist.
4.  **Rendered configuration from files** (§6): the mechanism, then the eight
    literals, with the functions' signatures unchanged.
5.  **The gateway component tree** (§4): `Gateway`, `DeviceServices`,
    `Container`, the typed census, the units' own dependencies, the flow-rule
    composition lifted out, and the vocabulary of §3 applied throughout.
6.  **The device-file provider** (§5): the connection onto the provider, the
    serialized-provider diff, the endpoint and pin as component outputs.
7.  **The stack program** (§8, §9): the configuration reading it has left, one
    shape per domain, exports in one block.
8.  **The adopted domain** (§10), and the caddy certificate (§6.3), both
    of which touch a live system's behavior and are worth landing alone.

Slices 1 to 3 are mechanical and reviewable by diff; 4 to 8 each rewrite their
own tests. None of them is a migration: the `physical` stack has no state, so
every rename in this document is a rename and not a replacement.
