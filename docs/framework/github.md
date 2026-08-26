# The Forge

How the GitHub side of the system is declared: the two repositories,
the Environments that partition CI's credentials, the gates and
protections that make the zero-diff proof load-bearing, and the two
single-purpose Apps. Everything here is `github` stack content
(pulumi.md §3.1); this document holds the *why*, the stack holds the
*what*.

It exists because the forge was the one part of the estate with no
declaration anywhere. The repository this file lives in is deployed by
rules that lived only in a settings page — nothing reviewed a change to
them, nothing detected drift in them, and rebuilding after an accident
meant remembering.

## 1. Applied from the workstation, never by CI

The credential this stack needs can edit branch protection and
environment gates. A workflow holding it means anything that reaches
`main` can also unguard `main` — including a dependency bump that
noop-automerge waved through. That collapses the credential partition
ci.md §2 is built on, where no stack holds more than its own layer.

The trade is cheap in the direction that matters: the forge changes a
few times a year, so a manual `pulumi up -s github` costs almost
nothing, while the credential would otherwise sit in CI permanently.
CI may **preview** this stack — drift detection is a read — and may
not apply it. The same reasoning the `physical` gate rests on
(ci.md §2), taken one step further: `physical` can root the gateway,
`github` can remove the gate that guards `physical`.

The credential itself is an account-root-scoped token from the
personal estate (credentials.md §2), used on the operator machine and
pushed to no slot. It is one of that section's account roots and is
acquired through the same chain as the rest: `credentials master github
remember` puts it on a machine, and because the reader is a template
rather than a script, the layer it lands in is the token file —
`.credentials/roots/github.token`, from which `mise.toml` materializes
`GITHUB_TOKEN`, falling back to the environment. Unlike the passphrase
it is derived from nothing, so no command here can recreate the
*value*: it comes from the estate each time, and its absence is what
stops this stack from being applied by accident.

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
    the reviewer gate in front of `up-physical` (ci.md §2) can exist,
    only because of it.
-   **`kluster-ops` stays private, so it will never have branch
    protection** on this plan. Nothing in the design asks it to: its
    `drill` Environment is deliberately ungated — its scope is the
    gate (credentials.md §4) — and the ops repo holds no stack.

Nothing in §3 is blocked by the plan any more, and §3 has been applied
(2026-08-25); what is still console state is there for the reasons §4
gives, not because the plan forbids it.

## 3. What is declared

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
    reviewer, is this stack's.
-   **The reviewer gate on `physical`**, with the operator as the
    reviewer and self-review permitted: the estate has one person, so
    self-review is the only review there is, and forbidding it would
    make the door impassable rather than stricter. Admin bypass is
    off for the same reason `enforce_admins` is on below.
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

### 3.1 The first apply

Only the two repositories need adopting. `Repository`'s create is a
create — it fails on a name that exists — so once, from the operator's
machine, before the first `up`:

```sh
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

## 4. What is not declared

-   **The Apps themselves, and their installations.** Creating an App
    and generating its private key is console-only (credentials.md
    §2), which is why those keys are seeds rather than derived
    credentials. Installation is console state too, for a measured
    reason: the endpoints that manage which repositories an
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
