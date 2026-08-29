# Style Rules

How code and prose in this repository are written, whatever the
language. The review gate in AGENTS.md holds every change to it.
Topics:

-   **[python.md](python.md)** — general Python readability, on top of
    the Google Python Style Guide.
-   **[pulumi.md](pulumi.md)** — architecture and style for the Pulumi
    programs: layering, components, providers, data placement.

The rules below apply to everything.

## Illegal states are unrepresentable

The design goal behind most rules here: a mistake should fail to
type-check, fail to parse, or be impossible to write — not be caught at
apply time, and not depend on a reviewer noticing. Values only correct
together are declared together; an API that must be called a certain
way accepts only that way; a state machine that has no "half done"
carries no flag that says so.

## Naming

**Descriptive over metaphorical.** A name states what the thing is, not
what it is like. No jargon-for-flavor when a plain compound says it:
device-services beats estate, initial-state-file beats seed. Rename
cost is paid once; decoding cost is paid by every reader.

**One term per concept, everywhere.** When two similar things coexist
(the ZeroTier overlay, the LAN networks), every class, method, and
variable says which one it means; a word that could mean either is
renamed until it cannot.

**Same shape for same role.** Modules exposing the same kind of entry
point use the same form and name. A reader should be able to guess a
module's API from its role.

## Comments and docs

**Plain speech, direct.** Say the constraint, then stop. No rhetorical
detours, no jargon where a plain word exists. If a comment needs three
sentences of scene-setting, the code or the name is wrong; fix that
instead.

**Generic code never names specific use-cases.** A reusable class
documents its contract; who uses it today belongs to the caller or the
design doc.

**Comments state constraints the code cannot show** — not history, not
what the next line does, not where an idea came from. Every artifact is
as-built: comments, commit messages, docs describe what is, without the
story of how it got there.

**Docs layer like the code.** `docs/framework/` documents mechanisms
(how this repo does Pulumi, CI, testing) and names no kluster design
decision; `docs/declarative/`, `docs/physical/` and `docs/cluster/` own
the design; this directory owns how things are written.

## Review gate

Every pull request passes an independent review against these rules
before merge — see AGENTS.md "Review stage". Major structural changes
go the other way around: an RFC under `docs/framework/` states the
desired end state and is approved before implementation starts.
