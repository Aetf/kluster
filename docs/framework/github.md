# The Forge

How the GitHub side of the system is declared: the two repositories,
the Environments that partition CI's credentials, the gates and
protections that make the zero-diff proof load-bearing, and the two
single-purpose Apps. Everything here is `github` stack content
([declarative/README.md](../declarative/README.md) §1); this document
holds the *why*, and the *what* is split between the census §3 opens
with and the stack program that declares from it.

It exists because the forge was the one part of this installation with
no declaration anywhere. The repository this file lives in is deployed by
rules that lived only in a settings page — nothing reviewed a change to
them, nothing detected drift in them, and rebuilding after an accident
meant remembering.

## 1. Applied from the workstation, never by CI

The credential this stack needs can edit branch protection and
environment gates. A workflow holding it means anything that reaches
`main` can also unguard `main` — including a dependency bump that
noop-automerge waved through. That collapses the credential partition
ci.md §3 is built on, where no stack holds more than its own layer.

The trade is cheap in the direction that matters: the forge changes a
few times a year, so a manual `pulumi up -s github` costs almost
nothing, while the credential would otherwise sit in CI permanently.
The same reasoning the `physical` gate rests on (ci.md §3), taken one
step further: `physical` can root the gateway, `github` can remove the
gate that guards `physical`.

So **no workflow touches this stack at all**, drift detection
included: the weekly `drift` matrix carries the four stacks CI
deploys and not this one (ci.md §3). Drift in the forge is read the
way an apply is prepared — `pulumi preview --refresh -s github` on the
machine that already holds the token — which is why leaving the stack
out of CI costs no freshness check, only the schedule of one.

The credential itself is an account-root-scoped token from the
personal estate (credentials.md §2), used on the operator machine and
minted by nothing in this repository. It is one of that section's
account roots and is acquired through the same chain as the rest:
`credentials root github remember` puts it on a machine, and because
the reader is a template rather than a script, the layer it lands in is
the token file — `.credentials/roots/github.token`, from which
`mise.toml` materializes `GITHUB_TOKEN`, falling back to the
environment. That paste is the only way the value ever arrives: the
state passphrase is random too, but it is escrowed, so a machine that
lost it runs one `recover` and has it back, while this token has no
copy anywhere the scripts can reach and must come from the personal
estate each time. Its absence is what stops this stack from being
applied by accident.

**The token is read at the line that builds the provider, and a run
without it stops there.** `pulumi_github` falls back to `GITHUB_TOKEN`
by itself and, failing that, runs *anonymously* — so a provider left
to configure itself turns a missing credential into a write refused
partway through an apply rather than into a refusal. The stack program
reads the variable and refuses by name when it is unset. What keeps the
value out of state in the clear is the generated provider, which marks
this input secret itself; a case over the declaration pins that rather
than the program wrapping it a second time. `Pulumi.github.yaml`
carries `pulumi:disable-default-providers: [github]`, which is the same
conversion for a resource that misses the explicit provider: an error
rather than a silent fallback.

That the token comes from the environment rather than from stack
configuration is this credential's own design, which is the
not-escrowed case of the credential-store rule in
[style/pulumi.md](../style/pulumi.md). Escrowing it would remove the
property the paragraph above rests on, that a machine which does not
already hold the token cannot apply this stack.

## 2. What the plan permits today

`kluster` is **public** (2026-08-25), which is what makes the rest of
this document buildable. Measured against the account: on GitHub Free a
*private* repository cannot have branch protection or rulesets at all —
the API answers `403 Upgrade to GitHub Pro or make this repository
public` for both — and the protection rules that make an environment a
*gate* (required reviewers, wait timers) are public-repository-or-paid
as well. Environments and environment secrets are the one half that
responds on a private repository.

Two consequences, both load-bearing:

-   **The public flip was a prerequisite for this repository's own CI
    security model**, not only for the arm64 runners images.yml needs
    (ci.md §4). "The preview was empty" can be a required check, and
    the reviewer gate in front of `up-physical` (ci.md §3) can exist,
    only because of it.
-   **`kluster-ops` stays private, so it will never have branch
    protection** on this plan. Nothing in the design asks it to: its
    `drill` Environment is deliberately ungated — its scope is the
    gate (credentials.md §4) — and the ops repo holds no stack.

Nothing in §3 is blocked by the plan any more, and §3 has been applied
(2026-08-25); what is still console state is there for the reasons §4
gives, not because the plan forbids it.

## 3. What is declared

**The roll lives in `conventions/forge.py`.** Which repositories
exist, whether each is public, which Environments each carries and in
what order the merge chain runs them, and which of those a reviewer
gates are one table there rather than constants in the stack program,
because a second program reads the same table: the `credentials`
command pushes a secret into every Environment the register names
(credentials.md §3), and a script may import `conventions` but nothing
a stack declares from. It also carries the labels a workflow branches
on — today `expect-changes` (ci.md §3) — held no shorter than what the
workflows actually read by a test, and declared as resources like
everything else below.

**A row carries what defines the entry, not what GitHub stores about
it.** The credential partition is defined in exactly these terms
(ci.md §3), which is why the branch policy and the gate are census
fields; what the stack program keeps is the repositories' own settings,
which define no entry and which nothing else reads — the required check
names, the descriptions, and the merge-strategy flags.

Two things follow from the table's shape. Whether the plan offers a
repository the public-only features of §2 is *derived* from its
visibility rather than written beside it, so the two cannot be left
disagreeing. And the account is one entry carrying both of the names it
answers to, its login and its numeric user id: an id is minted once and
never changes, so the reviewer gate below is named from a recorded
value rather than from a lookup on every run.

**Each repository is one `ManagedRepository`** (`components/forge`), so
the stack program is wiring rather than a list of resources: the
component owns one repository plus the resources that must come with
it and are invisible until they are needed — its vulnerability alerts,
the branch protection where the plan offers it, one label per census
entry, and one Environment per census entry. What differs between the
two repositories is census fields and parameters — visibility, the
Environments, whether required checks are named — rather than branches
in the component. It is the same shape, and the same name, as the
`dns` stack's `ManagedZone`.

-   **Repositories**: `kluster` (public) and `kluster-ops` (private;
    the notification and drill repo, ci.md §3) — visibility, the merge
    strategy, issue/wiki/project surface, vulnerability alerts, and
    secret scanning with push protection on the public one. Secret
    scanning is a public-or-paid feature, so asking for it on
    `kluster-ops` would be an API error rather than a stricter setting.
    Merges are **rebase only**: a squash rewrites authorship to the
    merging identity, which for an unattended merge is
    `noreply@github.com`, and a merge commit contradicts the linear
    history the branch protection asks for. Both carry
    `archive_on_destroy` and Pulumi's `protect`, so no run of this
    stack can delete the repository that contains it.
-   **Environments**: `dns`, `k8s-base` and `apps` (ungated, and
    deliberately with **no** deployment branch policy — `preview.yml`
    runs them from a pull request's own branch, so restricting them to
    protected branches would fail every preview); `physical-plan` and
    `physical` (protected branches only, since their credentials can
    root the gateway and never run a pull request's code); and `drill`
    in the ops repo (ungated — its scope is the gate,
    credentials.md §4). Which secrets each carries is the register's
    business (credentials.md §3); which exist, and which has a
    reviewer, is the table above.
-   **The reviewer gate on `physical`**, with the operator as the
    reviewer and self-review permitted: the installation has one person,
    so self-review is the only review there is, and forbidding it would
    make the door impassable rather than stricter. Admin bypass is
    off for the same reason `enforce_admins` is on below.
-   **Labels**: one resource per census entry, which today is
    `expect-changes` on `kluster` and nothing on `kluster-ops`. A
    workflow that reads a label nothing declares fails in the quietest
    way there is — the condition is simply never true, so the escape
    hatch is unavailable at the moment somebody needs it and nothing
    reports that the mechanism this document describes is absent.
    Every declared label carries the same color: these are switches a
    workflow reads rather than a taxonomy a reader browses, so a hue
    apiece would be meaning nobody put there.
-   **Branch protection on `main`**: `checks` and `changes` as required
    status checks, plus "branch must be up to date". Those two run on
    every pull request regardless of paths, which is what a required
    check has to do — one that only sometimes runs blocks a pull
    request forever. The `preview` matrix is deliberately **not**
    required: its check names carry the stack (`preview (dns)`), so
    pinning them freezes the stack list into a setting that no longer
    moves with the code, and its verdict is advisory — noop-automerge
    runs a zero-diff proof of its own rather than reading it
    (ci.md §3). `enforce_admins` is on, including for the account
    owner: a gate the only person who can open it walks around is a
    suggestion, and this one is why a merge to `main` implies a
    passing preview. Force pushes and deletion are off; history is
    linear.

### 3.1 What is adopted rather than created

Three of the resources §3 declares have a create that fails on what is
already on GitHub, so each is adopted by an `import` run from the
operator's machine rather than created. Two of them are in state and one
is owed.

**The two repositories are in state**, adopted before the apply §2
records:

```sh
# Already run. These are the commands that put the two entries in state.
mise x -- pulumi import -s github github:index/repository:Repository kluster kluster
mise x -- pulumi import -s github github:index/repository:Repository kluster-ops kluster-ops
```

The generated code `import` prints is ignored: the resources are already
declared here, and what is wanted is the state entry. Note that `import`
protects what it adopts (`--protect` defaults to true), which matches
the `protect` these two carry in the program anyway.

**`physical-plan` is not imported**, even though it already exists.
`RepositoryEnvironment`'s create is a `PUT`, so declaring it adopts the
existing Environment rather than colliding with it — and importing it
would have to reproduce this program's URN, which parents each
Environment under its repository. An import at the default (unparented)
URN produces a state entry the program cannot match, so the next preview
is "create the parented one, delete the imported one", and the delete is
blocked by the protection the import just applied. Recovering from that
is `pulumi state unprotect <urn>` then `pulumi state delete <urn>`: both
touch state only, leaving the Environment on GitHub for the create to
adopt.

**The `expect-changes` label is the one still owed, and it is owed
before the next `up`.** `IssueLabel`'s create is a plain create from
`pulumi-github` 6.15.0 onward — it calls GitHub's create-label endpoint
outright — so a create against the label, which is on `kluster` already
and declared here, is a 422. The lock names 6.14.0, whose create still
adopts, so what arms this is the next `uv lock --upgrade` rather than
the next apply, and the floor `pyproject.toml` names has no ceiling to
stand between the two. Importing removes the create instead, which is
why this is an import rather than a version ceiling: with the label in
state no apply creates it, and which create the provider would have made
stops mattering.

```sh
mise x -- pulumi import -s github github:index/issueLabel:IssueLabel kluster-expect-changes kluster:expect-changes \
    --parent 'repository=urn:pulumi:github::kluster-py::github:index/repository:Repository::kluster' \
    --protect=false
```

Its import id is `repository:name`. `--protect=false` because, unlike
the two repositories, the label carries no `protect` in the program.
`--parent` is what keeps it out of the trap above, because the program
declares every label under its repository — and the URN that flag names
is the one the repository's state entry carries now, unparented, because
the apply that puts it under the component has not been run. That is the
other half of *before the next `up`*: after that apply the parent is a
different URN. The alias below is what carries the repository across
that move, and the label the program declares inherits that alias from
its parent, which is what resolves the declaration onto the entry this
import writes. `pulumi stack --show-urns -s github` prints the URNs
state holds now.

**The import lands under the default provider, and nothing refuses
it.** `pulumi:disable-default-providers` (§1) is enforced where the
program is evaluated, and `import` never evaluates the program: it
computes the default provider's URN itself and reuses the state entry
that has it — the one §2's apply left behind, which is where the two
repositories sit — or builds one from the package configuration. So this
command succeeds quietly rather than stopping to be told which provider
to use. `--provider name=urn` is not the correction it looks like
either: the flag carries a URN and no inputs, so naming this program's
`kluster-github` would have the engine synthesize a provider with
neither owner nor token.

**What that provider move costs is the open part**, and the preview is
where it is read rather than here. The program signs every resource with
the explicit provider, so at the next `up` each imported entry changes
which provider it belongs to; whether that is a replacement depends on
how a provider answers a `DiffConfig` between the default's
configuration and this program's `owner` and `token`, and a replacement
of the label is a delete and a create, which is the 422 the import
exists to remove. The two repositories meet the same move holding
`protect`, which turns a replacement into a refusal rather than a
deletion. The recovery the `physical-plan` paragraph gives answers an
unparented import, which is a different cause.

The check is the preview that follows the import, and it is the whole of
the evidence there is before the apply. It is not empty and is not meant
to be: the component and the explicit provider are creates, the default
provider is a delete, the two repositories match through the alias below
rather than being created and deleted, and everything else §3 declares
that no apply has reached is a create. What the import buys is one
line's absence. A create, a delete or a replacement naming the label is
the failure it exists to prevent, and a replacement of either repository
is the other thing to stop on.

**The repositories' URNs moved when the component was introduced**, and
each repository carries an alias naming the URN it had while the stack
program declared it directly. Without one the preview would be "create
the parented one, delete the unparented one", and the delete is refused
by the `protect` above. One alias per repository covers its whole
subtree: everything the component declares is parented on the
repository rather than on the component, so each of those resources
inherits the alias and keeps the URN it already has. State carries the
unparented URNs today: the apply that moves them under the component is
the next one.

## 4. What is not declared

-   **The Apps themselves, and their installations.** Creating an App
    and generating its private key is console-only (credentials.md
    §2), which is why those keys are seeds rather than derived
    credentials. An App's installation is console state too, for a
    measured reason: the endpoints that manage which repositories an
    installation covers (`/user/installations/…`) reject a personal
    access token of either kind — they take only a user-to-server
    token from that App's own OAuth flow, an 8-hour credential the
    register has no tier for. Declaring one console page would cost a
    browser round trip before every apply, or turning off token
    expiry on both Apps (kluster-ops#11). Reading the state is cheap
    by comparison — an App can list its own installations with a JWT
    signed by the private key already in the kit — so an audit is the
    open option, not enforcement.
-   **Environment secret *values*.** Those are the `credentials`
    scripts' job, pushed into slots the register names. This stack
    creates the environment; the register fills it.
-   **The account roots.** They are a precondition of the system, held
    in the personal estate (credentials.md §2).
