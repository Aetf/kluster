# Threat Model

Who can act on this installation, which of them it defends against, what
it defends, and the test a security finding must pass to be worth
fixing. It spans the cluster, CI, the forge and the agent sessions, which
is why it sits at the root of `docs/` rather than in any one of their
documents.

Two registers stand beside it. The findings register,
[cluster/security-audit.md](cluster/security-audit.md), lists the
findings, each weighed against this page. The credential register,
[credentials.md](credentials.md), says what each credential reaches; this
page says whose hands it may be in. The cluster's own controls and
residuals under the model are
[cluster/architecture.md](cluster/architecture.md) §4.1. The argument
behind each line here, and the options weighed and rejected, are
[rfc-005](rfc/rfc-005-threat-model.md).

## 1. The stance

**The installation defends its owner's authority where it ends, not
against the owner.** One person owns every account, console and key
here, and whatever holds that authority is trusted with everything. What
is defended is the edge of that authority:

-   code the operator has not merged holds no credential in CI but those
    of the stacks a pull request previews (§3.1), and runs on the
    workstation as §3.3 records;
-   whatever is reachable from outside the operator's own devices
    authenticates before it acts, or is contained.

Inside that edge the defense is against mistakes, and it takes the form
of recovery (§3). A control against the owner cannot stand inside the
installation: whatever the owner can do, the owner can undo, so all such
a control catches is the owner's mistake, and a mistake is answered more
cheaply by being undoable than by being prevented at every step (§2.2).

**It defends against attacks that do not single this installation
out**, the opportunistic ones:

-   scanning, and published vulnerabilities;
-   a poisoned release of a package many projects use;
-   generic instructions injected into text an agent reads;
-   botnet firmware.

An adversary who studies this installation and spends effort on it alone
is accepted. The repository is public and documents every mechanism, so
no control here rests on being unknown, and nothing is built against a
targeted attacker beyond what the controls against opportunistic ones
already provide. The line is drawn by cost: the estate is one person's
and its data is a household's, and every control that has to beat a
patient adversary is paid for in the operator's time for as long as it
stands.

## 2. Who can act

Each class is defined by what it is, and the instances named are the
ones that exist today. A new App, platform or kind of session joins the
class its nature puts it in, without an edit here.

### 2.1 The operator

The owner's GitHub account, the provider consoles, the offline kit, and
the workstation that holds `.credentials/`. **Trusted, and its compromise
is accepted.** Each of these holds or can mint every credential the
installation has, so no control inside the installation can stand
between one of them and the rest. A compromise is answered outside the
installation: account recovery for the consoles, and the kit's full
rotation for everything minted (credentials.md §2.1).

A security rule that appears without the operator's knowing came from the
operator's hands, from a session's, by mistake or steered, or from a
compromised account. A detector stands against none of them: the first
two are answered by §2.2 and §2.3, and the last is accepted.

### 2.2 Mistakes made with the operator's authority

The operator's own, or a session's: a console edit, a command against the
wrong stack, a deleted bucket, a secret printed where the public can read
it. **In scope, and answered by recovery rather than prevention.** Most
mistakes are undoable or loud on the next routine run. Undoable means
backups that no automation key can delete, versioned buckets, state
dumps, and `protect`.

**A detector of its own is built only where the mistake would be silent
and would lose data or publish a secret.** A setting only the operator
can change, whose worst case is exposing a service that authenticates, is
not watched. A check written by the same hand, in the same change, as
the thing it checks is a second copy of one decision, and it catches
nothing that decision got wrong. What earns a detector is the mistake no
routine run would ever surface: a backup generation that silently stops
being restorable, a key that is already public. A declared resource whose
refresh shows a hand edit is not such a detector; it is what declaring
the resource does.

### 2.3 Agent sessions on the workstation

Every local Claude Code session. They hold what the operator's user can
read on the workstation:

-   `.credentials/` (credentials.md §4.4): the passphrase slots, the
    state backend's `operator` bundle, the libvirt identity, the account
    roots' token files on a machine with no desktop secret store, and
    the kit where the workstation keeps one;
-   the desktop secret store, which holds the account roots
    (credentials.md §2), the operator passphrase, and, wherever
    `credentials kit password remember` has run, the kit's master
    password;
-   the account's `gh` login and SSH key.

On a workstation whose store holds the kit's password, a session holds
every seed. **In scope as steerable, and defended at the session rather
than at the installation**, because the installation cannot tell a
session from the operator. What bounds a session's decisions is the
protocol's rule that only the operator and the session's dispatcher give
it instructions
([framework/dispatch.md](framework/dispatch.md) §1), and what bounds a
session steered past that rule is the harness's permission rules.
Neither bounds the code a session runs, which is §3.3's. The rule narrows
the path generic injected instructions take and does not close it: a rule
a session is told to follow is what a successful injection overrides.

### 2.4 Agent sessions in the cloud

Claude Code on claude.ai. They hold no credential
([framework/dispatch.md](framework/dispatch.md) §1.4). They push their
own branches as the operator's account, and they write issues, comments
and labels in the repositories attached to them, under that account.
**In scope.**

-   At the first half of the merge boundary their reach into
    credentials is an unmerged branch's (§3.1).
-   At the second half they are the operator's account, which no rule the
    forge offers tells from the operator, so today one merges any green
    pull request (§3.2). Under the clearance check the operator
    has ruled for (§3.2), one merges only a head
    that carries the clearance, since the key that clears one is not in a
    cloud session's reach; the account's administration of the
    repository, which can remove that requirement, is held by the
    protocol alone unless the session platform refuses the call.
-   Like anything that can push a branch, they reach `main` through the
    unattended route wherever its admission takes their head (§3.2).

What they write under the account is data to every other session outside
the shapes the protocol gives an instruction (framework/dispatch.md §1).
A cloud session can post something in the shape of a ruling or a brief,
and that is accepted: forging those shapes takes this protocol's decision
labels and hand-offs, which makes it an attack on this installation in
particular (§1).

### 2.5 Third parties that write into the repositories on their own schedule

Today that is Renovate, Mend's hosted App, installed on `kluster` with
write access to code, workflows, pull requests, issues, checks and commit
statuses. **Trusted, as a platform is (§2.7): its compromise is accepted,
and nothing here is built against it.** What it carries is not trusted:

-   the releases it proposes are §2.6's, held by their pins and their age,
    and at the merge boundary by what the unattended route admits (§3.2);
-   the text it writes, the upstream release notes in its pull requests
    and its Dependency Dashboard, is data (framework/dispatch.md §1), since
    in the poisoned-release case of §2.6 the attacker wrote the notes.

It does what the configuration on the default branch of the repository
it updates tells it, so whatever can write that branch directs a trusted
App. On `kluster` that is a merge (§3.2). It is not installed on the
operations repository, where the dispatch App's token can write the
default branch: nothing there that can write a workflow takes instructions from a
file that token can write (cluster/architecture.md §4.3).

### 2.6 Code the installation runs and did not write

Every release the installation runs and did not write:

-   the Python packages in `uv.lock`;
-   GitHub Actions;
-   mise, and the tools `mise.toml` pins;
-   the ZeroTier client the CI member installs;
-   the Pulumi providers and the bridged SDKs;
-   the Helm charts and the manifests the cluster installs;
-   container and OS images, the state backend's among them.

**In scope, and the likeliest outside actor.** A reader cannot see a
poisoned package in a lock diff, so reading is not the control. The
controls are a pin that cannot move under its name, a release old enough,
and the merge boundary.

**A pin that records its bytes.** A pin of this class carries a hash or
a digest that the download is checked against, so a release asset
uploaded again under the same version is refused rather than installed.
Today:

-   the Python packages, by the hashes `uv.lock` records;
-   the Actions, by commit;
-   the tools `mise.toml` pins, by the sha256 `mise.lock` records for each
    one's artifact, which `mise.toml`'s `locked` makes binding;
-   the container images the `versions:` block of `Pulumi.yaml` names, by
    digest: the gateway's root filesystems and local-path-provisioner's
    images; and the bases of the self-built images
    ([framework/ci.md](framework/ci.md) §4);
-   a Helm chart its publisher serves from an OCI registry, by the digest
    of its manifest, which Helm pulls it by: `chart-cilium`,
    `chart-cert-manager`, `chart-cloudnative-pg`,
    `chart-plugin-barman-cloud`, `chart-victoria-metrics-k8s-stack`,
    `chart-node-feature-discovery`, `chart-reloader`,
    `chart-sealed-secrets`, `chart-intel-device-plugins-operator` and
    `chart-intel-device-plugins-gpu`;
-   a manifest fetched as a release asset, by its sha256, which the fetch
    checks: `manifest-gateway-api`, the Gateway API definitions;
-   the state backend's Fedora CoreOS artifact and its `age` binary, by
    the sha256 beside each pin in `kluster.lib.state_backend.settings`,
    and the Helm binary `update_crds` renders with, by the sha256 beside
    its pin.

**A pin that names a version alone**, and so moves under such an upload.
Each of these carries its reason, and these are accepted residuals:

-   a Helm chart served only from an HTTP repository, which offers
    nothing to check a download against: `chart-volsync` and
    `chart-metrics-server` (`Pulumi.yaml` says why beside them);
-   the Pulumi provider plugins, the bridge and the providers it fetches
    (`Pulumi.yaml` says why beside its `packages:` block);
-   the Talos factory images, fetched by schematic and release: the public
    Image Factory publishes no checksum, and OCI imports the cloud image
    from a URL with no digest.

These await the operator's ruling, each named with its issue:

-   an image a chart deploys, which is pinned as that chart's own values
    pin it: the chart's digest holds the reference and not the bytes
    behind a tag (Aetf/kluster-ops#503);
-   mise itself, whose version every workflow hands `jdx/mise-action`
    with no sum for the action to check, and which a cloud session
    installs from `npm` by version (Aetf/kluster-ops#504);
-   the ZeroTier client, installed from ZeroTier's apt repository by
    version, with the repository's signing key pinned by fingerprint, so
    an upload its publisher signs again under the same version passes
    (Aetf/kluster-ops#504);
-   the state backend's Postgres image, `postgres:17`, a tag naming a
    major line (Aetf/kluster-ops#504).

**A release no pin holds**, which arrives on its publisher's schedule,
with no age and no merge. These await the operator's ruling too
(Aetf/kluster-ops#504):

-   the state backend's Postgres minor releases, which
    `podman-auto-update` pulls under that tag;
-   the state backend's Fedora CoreOS updates, which Zincati applies to
    the running box.

[operations.md](operations.md) §1 is the census of who moves each pin.

**A release old enough** that a compromise of it, aimed at everyone who
installs it, has usually been caught upstream. Every route CI executes
waits a week after a release is published, or, where its source reports
no release time, for a person on the dependency dashboard; uv holds what it resolves
to the same week. The container images and
the Helm charts, which the cluster only pulls, wait for none, and that is
an accepted residual: the cluster runs what it pulls with whatever the
workload holds, so a poisoned image or chart release reaches it the day
it is published, held by its pin and by whatever reading its bump gets.
Which routes wait, and why a week, is the comment on `minimumReleaseAge`
in `renovate.json5`.

**The merge boundary**, whose first half keeps an unmerged pin in CI away
from every credential but those of the stacks a pull request previews,
where it holds (§3.1). It
does not reach the workstation, where a session that runs a branch's
dependencies runs them with the operator's credentials (§3.3).

A compromise planted to outlast the waiting period is accepted.

### 2.7 The platforms the installation runs on

GitHub (the repositories, Actions and their secrets), OCI, Cloudflare,
Backblaze B2, ZeroTier Central, the Sidero discovery service, the
platform Anthropic runs cloud sessions on, and Mend, which runs Renovate
(§2.5). **Their compromise is accepted, and their loss is in scope.** Each
already holds whatever an attacker would want from it. An outage or a
closed account is an availability event, answered by backups kept off the
platform that failed ([cluster/storage.md](cluster/storage.md) §5).

### 2.8 The installation's own GitHub Apps

The dispatch App and the trigger App. **They are not actors of their
own.** What an App's key reaches counts as reachable by whatever can
start a run that reads the key. A key held as a repository secret, as
both of these are (credentials.md §3), is read by any run in its
repository, unmerged ones included; a key held only on the workstation
and where `main`'s own code runs is read by no run of an unmerged ref.

On `kluster` the dispatch App's token carries Contents write. Today that
merges a pull request whose required checks are green and that changes
no workflow file; under the clearance check (§3.2) it merges only a head
that carries the clearance. Either way it pushes to every unprotected branch,
a Renovate branch among them, and the regeneration workflow hands that
token to the code of the branch it regenerates (§3.2). On the operations
repository, what a token reaches is held by that repository's fence
(cluster/architecture.md §4.3).

### 2.9 Anyone on the internet

They reach every public address: the cloud nodes, the balancer, the state
backend, and the gateway's WAN side. They also reach the public
repository, where any account can comment on an issue or a pull request
and open a pull request from a fork. **In scope for opportunistic
attacks.** The controls:

-   every exposed port either authenticates before it acts, or is a public
    service by design that runs contained (cluster/architecture.md §4.1);
-   every exposed port runs a maintained release;
-   a fork's run receives no secret (framework/ci.md §3);
-   text an outsider writes on the forge is data to every session
    (framework/dispatch.md §1).

A port moved to an unusual number quiets a log. It is not a control.

### 2.10 Devices on the home network and the overlay

Grouped by who controls their software:

-   **The server LAN, the container VLAN and the personal overlay
    members**: hosts whose software the operator chose, which are the
    household's computers and phones, the homelab host, and the gateway's
    own container services, the resolvers among them. **Trusted at the
    network layer**: an address one of them spoofs, or a reply one of them
    forges, is accepted.
-   **IoT devices**, on the IoT VLAN, whose firmware the operator does not
    control. **In scope**, as the likeliest compromised host at home.
    Their VLAN reaches the cluster's pool through an enumerated allow
    (cluster/security-audit.md, M2).
-   **The cluster VLAN.** The node on it is the operator's, but what runs
    on it is §2.6's and reachable from §2.9, so it is not trusted at the
    network layer. The cluster's own controls (cluster/architecture.md
    §4.1) answer for it.
-   **The overlay's CI members.** **In scope**, because an identity can
    leak where anyone can take it, a public Actions log among them. Each
    is confined by its node address, which its holder cannot withhold
    ([physical/gateway.md](physical/gateway.md) §2.3).

## 3. What is defended

-   **The merge boundary**, in two halves, both required:
    -   *an unmerged ref runs with no credential but those of the stacks a
        pull request previews* (§3.1);
    -   *a ref reaches `main` only at a head that the operator or the
        unattended route has passed, enforced by the forge rather than by
        the protocol* (§3.2).

    The boundary exists to hold every actor other than the operator that
    can push a branch or hold a token: cloud sessions, whatever reads the
    dispatch App's key, and a poisoned release riding a pin, Renovate's
    bumps included.
-   **The network boundary.** Every service reachable from the internet or
    from the IoT VLAN authenticates before it acts, or is contained, and
    every one runs a release that is kept current.
-   **Recovery.** Mistakes, and the loss of a platform, are answered by
    what can be restored: off-platform backups that no automation key can
    delete, versioned state, and the kit.

The first half of the merge boundary holds for the operator stacks'
credentials, and for `physical`'s once it has moved and the stack passphrase it was under has been rotated out of every Environment (§3.1). The forge does not enforce the second half today (§3.2).
The workstation is outside the boundary by construction (§3.3).

### 3.1 The first half: what an unmerged ref's runs hold

> A ref that has not merged runs, in a preview or in any workflow it
> adds, with the configuration secrets of `dns`, `k8s-base` and `apps`,
> the `dns` overlay membership, and read and write access to every
> stack's state; `physical`'s credentials, and the operator stacks', never
> reach it.

That is the half once `physical` has moved and the stack passphrase it
was under has been rotated out of every Environment. `physical`'s configuration
moves under a passphrase of its own, which only `physical-plan` and
`physical` receive. Those Environments take protected branches only, so
a job of a pull request or of a branch push that names either is refused
before any step of it runs: GitHub documents this, and the move's first
contact observes it ([framework/github.md](framework/github.md) §1,
framework/ci.md §3). `Pulumi.physical.yaml` is under that passphrase
(Aetf/kluster-ops#487). The move comes before `physical`'s first `up`, because the backend keeps every
checkpoint written before it in rows those runs read. Git history keeps
the ciphertexts `physical`'s configuration held under the stack
passphrase (credentials.md §1 rule 6), so this half holds for `physical`
once the stack passphrase those ciphertexts are under has been rotated
out of every Environment: `credentials derived sync --only
pulumi-passphrase` replaces it there, once `credentials derived
pulumi-passphrase re-encrypt` has moved every stack under it
(credentials.md §4.2). A pull request's runs then hold only
a generation that opens none of them, the earlier one being assumed not
leaked.

`dns`, `k8s-base` and `apps` share the stack passphrase: their
Environments take any branch for the previews, so whatever reaches one
reaches all three, and a passphrase apiece would hold nobody out of
anything. Their configuration secrets include the cluster's kubeconfig,
once `credentials derived sync --only kubeconfig` has copied it into
`k8s-base` and `apps` (credentials.md §3).

Pull requests keep their previews: for `dns`, `k8s-base` and `apps` a
preview is the only rendered diff anyone reads before the change applies,
and the unattended route's zero-diff proof is a preview too
(framework/ci.md §3). The state access cannot be narrowed: the backend
creates its table on every open, so no role it accepts can only read
([physical/state-backend.md](physical/state-backend.md) §2). A rewrite of
`physical`'s state shows in `plan-physical` ahead of its reviewer gate; a
rewrite of another stack's is acted on by that stack's next `up`.

### 3.2 The second half: who can merge

**The forge does not enforce it today.** `main` requires `checks` and
`changes` on an up-to-date branch, for administrators too, and sets no
review, no restriction on who updates it, and no ruleset
(framework/github.md §3). Merging a pull request needs only Contents
write, and a pull request that changes a workflow file needs the
`workflows` permission as well, which Renovate holds and neither a
workflow's token nor the dispatch App does. So any token that carries
Contents write merges a pull request once its required checks are green,
within that limit:

-   **the operator's account, and every session acting as it**, cloud
    sessions included: the rule that a cloud session never merges
    (framework/dispatch.md §1.4) is a rule, which a steered session does
    not keep;
-   **the dispatch App**, whose token the regeneration workflow
    (`sdk-regenerate.yml`) persists in the checkout where it then
    resolves, installs and tests the Renovate branch it regenerates, so a
    poisoned release in that branch runs holding a token that merges any
    green pull request that changes no workflow file;
-   **a workflow token that carries Contents write while a branch's code
    runs**, as `noop-automerge.yml` grants every job, `prove` among them,
    which installs and previews the branch's dependencies. A branch that
    adds or edits a workflow can raise its own token the same way, and
    since no token without the `workflows` permission merges a pull
    request that changes a workflow file, such a branch merges other pull
    requests, not itself.

Renovate's App holds the same and is trusted (§2.5); the configuration on
`kluster`'s `main` leaves merging to CI (`renovate.json5`). Worded:

> Any token with Contents write merges a pull request whose required
> checks are green and, unless it also holds the `workflows` permission,
> that changes no workflow file: the account and every session acting as
> it, the dispatch App, whose token a poisoned release holds while the
> regeneration workflow runs it, and a workflow token that carries
> Contents write while a branch's code runs. The second half of the merge
> boundary is held by the dispatch protocol alone.

**Two routes merge with nobody present, and both stay**: the
dispatcher's `gh pr merge --rebase --auto`, and the unattended route,
which merges a Renovate bump behind its allow-list and its zero-diff proof
with nobody reading it (framework/ci.md §3).

**The operator's ruling closes this half with a clearance check**, which
is not built yet and has a design of its own (Aetf/kluster-ops#488). The
check is one more status check `main` requires, accepted from one App
alone, whose token writes checks and commit statuses and nothing else, so
it clears a head and merges none. The App's key is held on the
workstation and in an Environment only `main` deploys to, and no run of
an unmerged ref reads either. The dispatcher clears the head it compared
and then pushed; the unattended route's merge moves into a job of
`main`'s own code, which applies the route's admission again and clears
the head the proof ran on. A check run belongs to a commit, so a push
after the clearance leaves the new head blocked until it is cleared in
turn, and nothing, the account included, merges a head nobody cleared.
Under the check, the dispatch App and a workflow token each still merge,
but only a head the dispatcher or the route has cleared, and two
residuals stay. The cloud sessions':

> A cloud session merges only a head that carries the clearance; as the
> operator's account it can remove the requirement itself, held there
> only by the protocol's rule that it never changes repository settings,

unless the session platform refuses that call, in which case they are
held at both halves. The unattended route's:

> The unattended route clears, with nobody reading it, every head its
> admission takes, so whatever can push such a head reaches `main` by
> it: the account and every session acting as it, and whatever reads the
> dispatch App's key.

Renovate's own bumps are what the route exists for, and what they carry
is §2.6's. The route's admission is that residual's bound
(framework/ci.md §3). A workflow token is not among its actors: a run
that its push or its pull request causes waits for someone with write
access to approve it, so no proof runs on a head it pushed.

### 3.3 The workstation: code a session runs

**Unmerged code runs on the workstation with every credential it
holds.** Every local session, and every process it starts, runs as the
operator's user, which reads `.credentials/` and the desktop secret store
(§2.3). Ordinary session work runs releases nobody has read, before any
merge:

-   the gate, run on a branch that moves a pin, as when a reviewer holds a
    dependency bump;
-   a builder's own `uv lock --upgrade-package` or `uv add`;
-   `mise install` after an edit to `mise.toml`.

What holds a poisoned release there is its age, and the age holds at
resolution: uv's `exclude-newer` holds every `uv lock`, whoever runs it,
a session, CI or Renovate's lock-file maintenance, so a Python release
younger than the span enters the lock only where the operator admits it
by name. A pin a person or a session moves by hand in `mise.toml` or
`Pulumi.yaml` waits for nothing. Accepted, worded:

> A session that runs a branch's dependencies runs them with every
> credential the workstation holds, the seeds among them where the secret
> store keeps the kit's password; a poisoned release is held there by its
> age, where it has one, and a pin a person or a session moves by hand has
> none.

The hole it leaves, a compromise that outlasts the age, is the one §2.6
already accepts.

## 4. The test

A security finding is worth fixing when it passes all three clauses:

1.  **an in-scope actor can reach it;**
2.  **reaching it gives that actor something it does not already hold by
    a path the model accepts;**
3.  **the fix costs less than what it protects**, counting the cost to
    build, the cost to keep, and the operator's time.

A finding that fails the test is recorded as accepted, naming the actor
it answers to and the clause it fails. A finding whose only actor is a
targeted adversary fails the first clause. A disputed third clause is
the operator's ruling, not a thread. Reviewers hold every security
finding to the test ([framework/dispatch.md](framework/dispatch.md) §3).

The second clause is the one most easily skipped: a lock on a door
protects nothing from an actor already in the room. The ZeroTier Central
token is a configuration secret of the `physical` stack (credentials.md
§3), and it writes the overlay's flow rules, so no flow rule constrains
whatever opens that configuration. The flow rules are worth what they do
against an actor who holds an overlay identity and nothing else, and
against that actor they are the whole defense.
