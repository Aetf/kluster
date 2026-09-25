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
few times a year, so a manual `mise run github up` costs almost
nothing, while the credential would otherwise sit in CI permanently.
The same reasoning the `physical` gate rests on (ci.md §3), taken one
step further: `physical` can root the gateway, `github` can remove the
gate that guards `physical`.

So **no workflow touches this stack at all**, drift detection
included: the weekly `drift` matrix carries the four stacks CI
deploys and not this one (ci.md §3). Drift in the forge is read the
way an apply is prepared — a `mise run github preview --refresh` on
the machine that holds this stack's passphrase, which no job does —
which is why leaving the stack out of CI costs no freshness check, only
the schedule of one.

The credential itself is **a provider credential like every other one
here**, and it lives where the rest of them live: a secret in this
stack's committed configuration, `githubAdminToken` in
`Pulumi.github.yaml` (credentials.md §3). What makes it unlike the
Cloudflare or OCI credentials beside it is only how the value is
obtained. GitHub publishes no API that creates a personal access token,
so no seed mints it and no command can rotate it unattended: it is made
on the account's own settings page, `credentials derived github-admin
record` takes it from there into the stack file, and a rotation is that
console visit plus that command plus deleting the superseded token on
the same page. It is not an account root — the account root is the
GitHub login behind that page, which stays in the personal estate and
which nothing in this repository opens.

**The token is read at the line that builds the provider, and a run
without it stops there.** `pulumi_github` falls back to a `GITHUB_TOKEN`
in the environment by itself and, failing that, runs *anonymously* — so
a provider left to configure itself turns a missing credential into a
write refused partway through an apply rather than into a refusal. The
stack program requires the config key and refuses by name when it is
absent, naming the command that fills it. What keeps the value out of
state in the clear is the generated provider, which marks this input
secret itself; a case over the declaration pins that rather than the
program wrapping it a second time. `Pulumi.github.yaml`
carries `pulumi:disable-default-providers: [github]`, which is the same
conversion for a resource that misses the explicit provider: an error
rather than a silent fallback.

**This stack's configuration is encrypted apart from the rest of the
estate, and that is what keeps CI away from the token.** The estate
passphrase is an Environment secret in *every* Environment, because
every job runs a `pulumi` command — so a config secret under that
passphrase is readable by anything CI can start. That is a wider set
than it sounds: `dns`, `k8s-base` and `apps` are `ANY_BRANCH` and
ungated by design, since `preview.yml` runs them on pull-request
branches, and on a `pull_request` event the workflow definitions come
from the pull request's own branch. **Anybody who can push a branch to
this repository could therefore write a workflow that claims one of
those Environments and prints whatever that passphrase opens** — no
merge, no review, no automerge. Under the estate passphrase, that would
include a token which can delete the branch protection guarding `main`,
and ci.md §3's partition — that no workflow may hold the credential
which writes its own Environment's secrets — would be a statement about
nothing.

So the `github` stack has a **passphrase of its own**
(credentials.md §3): generated, escrowed to the kit like the estate's,
written to a workstation slot, and pushed to no Environment at all.
`Pulumi.github.yaml` is as public as any other stack file and its
ciphertext is committed; what is not public is the key, and no job has
it.

Two mechanisms hold that, because either alone is one accident from
gone, and each has a test: no workflow points a `pulumi` command at this
stack (a census over `.github/workflows/` and `.github/actions/`, the
same idiom the label and author censuses use), and the register row for
this passphrase reaches no GitHub secret (a case over the slot map). A
`preview` would be as bad as an `up`: reading this stack's config at all
means holding the passphrase, and a workflow that held it would have it
in an Environment.

**`PULUMI_CONFIG_PASSPHRASE` is process-global, so "a passphrase per
stack" is a property of how a stack is invoked.** Every `credentials`
command resolves it from the stack it is acting on, in one place — a
`Stack` derives its own environment from its own name, so no call site
can pair one stack with another's passphrase. A `pulumi` run by hand
gets the same property from a task. `mise.toml` exports this stack's
passphrase as `KLUSTER_GITHUB_PASSPHRASE` rather than as the ambient
`PULUMI_CONFIG_PASSPHRASE`, and `mise run github <pulumi args>` runs
`pulumi` with the stack fixed to `github` and that value passed in, so
the stack and its passphrase are named in one place:

    mise run github preview
    mise run github up
    mise run github config get githubAdminToken

The plain form is the whole of it for everything this page runs. Only
a command carrying a `--`, `-h` or `--help` of its own needs more,
because mise takes those words for itself: they go after a leading
`--`, as in `mise run github -- up --help`. The task refuses an argument
that names a stack of its own (`-s`, `--stack`); the one it runs against
is never the caller's to choose. `mise.toml` carries the rest of its
contract.

Getting it wrong is not silent. A bare `pulumi … -s github` meets the
estate passphrase, and `encryptionsalt` is a verifier, so `pulumi`
answers `error: incorrect passphrase`, exits non-zero and writes
nothing — for a read and for a write alike. (What would re-key a stack
quietly is any `pulumi` command against a file with *no* salt, a
`preview` as much as a `config set --secret`: with nothing to verify
against, `pulumi` mints a salt from the ambient passphrase and writes it
in, which is why credentials.md §4.2 forbids deleting that line.) A
machine holding no passphrase for this stack is refused one step earlier
still. The task will not start `pulumi` with `KLUSTER_GITHUB_PASSPHRASE`
empty, which is what it resolves to on such a machine, and inside a
`jj` workspace, where the slots do not answer and only a value the
caller exported gets through; a `credentials` run names the stack and
the command that fills it. Neither lets `pulumi` refuse at the
far end of whatever was in progress.

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

Nothing in §3 is blocked by the plan anymore, and §3 has been applied
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
a stack declares from. It also carries the switches a workflow
branches on: the labels it reads — `expect-changes` on this
repository (ci.md §3), `alert` on the ops repository (operations.md
§4) — and the identities it tests a pull request's author against — today
renovate's — and the repository variables it reads, today the dispatch
App's client id (`DISPATCH_APP_CLIENT_ID`, ci.md §3). Each is held no
shorter than what the workflows actually read by a test. A label and a
variable are declared as resources like everything else below; an
author is named to nothing, because the only thing that reads one is a
workflow comparing a login against a string. The Apps themselves are
console-made (§4), and what the table holds of one is its public
identity — slug and client id, `conventions.forge.App` — and which
repositories it is installed on, recorded on each repository's row: a
variable that hands an App's client id to a token mint is a mint that
fails unless the App is installed there, and a test holds the two
fields of the row to each other.

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
the branch protection where the plan offers it, one label and one
variable per census entry, and one Environment per census entry. What
differs between the
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
    `expect-changes` on `kluster` and `alert` on `kluster-ops`, the
    label the ops repository's dispatch handler puts on the alert
    issues it opens and finds them by. A
    workflow that reads a label nothing declares fails in the quietest
    way there is — the condition is simply never true, so the escape
    hatch is unavailable at the moment somebody needs it and nothing
    reports that the mechanism this document describes is absent.
    Every declared label carries the same color: these are switches a
    workflow reads rather than a taxonomy a reader browses, so a hue
    apiece would be meaning nobody put there.
-   **Repository variables**: one `ActionsVariable` per census entry,
    which today is `DISPATCH_APP_CLIENT_ID` on `kluster` — the dispatch
    App's client id, which `sdk-regenerate.yml` and `alert.yml` hand to
    the action that mints the App's token beside the key (ci.md §3) —
    and nothing on `kluster-ops`. The value is public: a client id names the App
    and authenticates as nothing, so it is a census fact in the clear
    (`conventions.forge.DISPATCH_APP`), and the variable is spelled from
    it rather than typed into the console. A value that would have to
    be a secret is never a variable; it is a register row
    `credentials derived sync` pushes (credentials.md §3). The failure a
    declared variable prevents is the label's: a workflow reading a
    variable nobody set receives an empty string, and the step that
    needed it fails on its own terms. The variable did not exist on
    GitHub before this program declared it, so its create is a plain
    create; one set by hand first would be refused as a duplicate at
    the create, and an `import` under the repository's URN would be
    owed the way §3.1's label import is.
-   **Branch protection on `main`**: `checks` and `changes` as required
    status checks, plus an up-to-date branch. Those two run on
    every pull request regardless of paths, which is what a required
    check has to do — one that only sometimes runs blocks a pull
    request forever. The `preview` matrix is deliberately **not**
    required: its check names carry the stack (`preview (dns)`), so
    pinning them freezes the stack list into a setting that no longer
    moves with the code, and its verdict is advisory — noop-automerge
    runs a zero-diff proof of its own rather than reading it
    (ci.md §3). `enforce_admins` is on, including for the account
    owner: a gate the only person who can open it walks around is a
    suggestion, and this one is why a merge to `main` implies a green
    `checks` and `changes` on an up-to-date branch. It implies **no**
    preview, and not for a code change either: the `preview` matrix is
    not a required check (above), so nothing in branch protection makes
    its verdict a condition of merging, and a pull request whose two
    required contexts are green is mergeable while the rest of the
    matrix is red (ci.md §5). That a change no stack program reads does
    not run the matrix at all is a second reason, and the one that lets
    `noop-automerge` merge such a change on the required checks alone
    (ci.md §3). Force pushes and deletion are off; history is linear.

### 3.1 What is adopted rather than created

Three of the resources §3 declares have a create that fails on what is
already on GitHub, so each is adopted by an `import` run from the
operator's machine rather than created. Two of them are in state; the
third's paragraph says how to read whether it is.

**The two repositories are in state**, adopted before the apply §2
records:

```sh
# Already run: the two imports that put these entries in state.
mise run github import github:index/repository:Repository kluster kluster
mise run github import github:index/repository:Repository kluster-ops kluster-ops
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
is `mise run github state unprotect <urn>` then
`mise run github state delete <urn>`: both
touch state only, leaving the Environment on GitHub for the create to
adopt.

**The `expect-changes` label is the one an import can still owe, and
it is owed before the first `up` on a provider whose create no longer
adopts.**
`IssueLabel`'s create is a plain create from `pulumi-github` 6.15.0
onward — it calls GitHub's create-label endpoint outright — so a create
against the label, which is on `kluster` already and declared here, is a
422. The lock names 6.14.0, whose create still adopts, so what arms this
is the next `uv lock --upgrade` rather than the next apply, and the
floor `pyproject.toml` names has no ceiling to stand between the two.
Importing removes the create instead, which is why this is an import
rather than a version ceiling: with the label in state no apply creates
it, and which create the provider would have made stops mattering.
Whether it is in state already is read from
`mise run github stack --show-urns` before the command is run rather
than from this page: a create on 6.14.0 adopts, so an `up` on that
version that reached the label is what put it in state, and the import
is owed only while nothing has.

```sh
mise run github import github:index/issueLabel:IssueLabel kluster-expect-changes kluster:expect-changes \
    --parent 'repository=urn:pulumi:github::kluster-py::kluster:components:forge:ManagedRepository$github:index/repository:Repository::kluster' \
    --protect=false
```

Its import id is `repository:name`. `--protect=false` because, unlike
the two repositories, the label carries no `protect` in the program.
`--parent` is what keeps it out of the trap above, because the program
declares every label under its repository, and the URN that flag names
is the repository's as state holds it: under the component, so the type
chain runs `ManagedRepository$Repository`. `mise run github stack
--show-urns` prints it, and that print rather than this page is what the
flag is copied from.

**The import lands under the default provider, and nothing refuses
it.** `pulumi:disable-default-providers` (§1) is enforced where the
program is evaluated, and `import` never evaluates the program: it
computes the default provider's URN itself and reuses the state entry
that has it, or builds one from the package configuration. So this
command succeeds quietly rather than stopping to be told which provider
to use. `--provider name=urn` is not the correction it looks like
either: the flag carries a URN and no inputs, so naming this program's
`kluster-github` would have the engine synthesize a provider with
neither owner nor token.

**What that provider move costs is read from the preview that follows
the import**, and nowhere else. The program signs every resource with
the explicit provider, so at the next `up` the imported entry changes
which provider it belongs to; whether that is a replacement depends on
how the provider answers a `DiffConfig` between the default's
configuration and this program's `owner` and `token`, and a replacement
of the label is a delete and a create, which is the 422 the import
exists to remove. What the import buys is one line's absence: a create,
a delete or a replacement naming the label is the failure it exists to
prevent, and the preview is stopped on rather than applied.

**State holds every URN the program declares, and the program carries no
alias.** Each repository sits under its `ManagedRepository`, and the
branch protection and the Environments sit under names that carry the
repository's ([style/pulumi.md](../style/pulumi.md)'s child-name rule),
so a preview of this program against it names no rename and no
replacement. An alias is how a move onto that state is made — a
re-parenting or a rename lands as a same-URN update rather than as a
create beside a delete, which `protect` refuses on the repositories and
which, on an Environment, discards its secrets — and it is carried only
until the apply that moves the entry, as style/pulumi.md says. A rename
or a replacement in a preview of an unchanged program is the tell that a
move was applied without one, or that an alias was dropped before its
apply.

## 4. What is not declared

-   **The Apps themselves, and their installations.** Creating an App
    and generating its private key is console-only (credentials.md
    §2.2), which is why each key is a derived row that is recorded and
    escrowed rather than minted (credentials.md §3). An App's
    installation is console state too, for a measured reason: the
    endpoints that manage which repositories an installation covers
    (`/user/installations/…`) reject a personal access token of
    either kind — they take only a user-to-server token from that App's
    own OAuth flow, an 8-hour credential the register has no tier for.
    Declaring one console page would cost a browser round trip before
    every apply, or turning off token expiry on both Apps
    (kluster-ops#11). Reading the state is cheap by comparison — an App
    can list its own installations with a JWT signed by its private
    key, which the escrow already holds — so an audit is the open
    option, not enforcement. What the census records of an App is
    its public identity and where it is installed (§3); nothing here
    creates or installs one.
-   **Environment secret *values*.** Those are the `credentials`
    scripts' job, pushed into slots the register names. This stack
    creates the environment; the register fills it.
-   **The account roots.** They are a precondition of the system, held
    in the personal estate (credentials.md §2).
