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
pushed to no slot.

## 2. What is declared

-   **Repositories**: `kluster` (this one) and `kluster-ops` (private;
    the notification and drill repo, ci.md §5) — visibility, merge
    strategy, secret scanning and push protection, and the settings
    that must not silently change.
-   **Environments and their gates**: `physical-plan` (ungated, plan
    only), `physical` (reviewer-gated, applies), `dns`, `k8s-base`,
    `apps`, and `drill` in the ops repo (ungated — its scope is the
    gate). Which secrets each carries is the register's business
    (credentials.md §3); which environments *exist*, and which have a
    reviewer, is this stack's.
-   **Branch protection on `main`**: required checks and up-to-date
    branch, which is what makes "the preview was empty" a statement
    about the code that will be on `main` rather than about a stale
    branch.
-   **App installations**: which repositories the dispatch and trigger
    Apps are installed on, and with what repository selection.

## 3. What is not declared

-   **The Apps themselves.** Creating an App and generating its
    private key is console-only (credentials.md §2), which is why
    those keys are seeds rather than derived credentials. The stack
    declares where an existing App is installed, not that it exists.
-   **Environment secret *values*.** Those are the `credentials`
    scripts' job, pushed into slots the register names. This stack
    creates the environment; the register fills it.
-   **The account roots.** They are a precondition of the system, held
    in the personal estate (credentials.md §2).
