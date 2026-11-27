# Style Rules

How code and prose in this repository are written, whatever the
language. The review gate holds every change to it
([framework/dispatch.md](../framework/dispatch.md) §3). Topics:

-   **[python.md](python.md)** — general Python readability, on top of
    the Google Python Style Guide.
-   **[pulumi.md](pulumi.md)** — architecture and style for the Pulumi
    programs: layering, components, providers, data placement.
-   **[testing.md](testing.md)** — what a test may assert, and what it
    may write down to assert it.

The rules below apply to everything.

## Illegal states are unrepresentable

The design goal behind most rules here: a mistake should fail to
type-check, fail to parse, or be impossible to write — not be caught at
apply time, and not depend on a reviewer noticing. Values only correct
together are declared together; an API that must be called a certain
way accepts only that way; a state machine that has no "half done"
carries no flag that says so.

## A membership is a definition, not a list

A set the code or a test acts on is stated by what makes something a
member, not by naming the members. A list misses the member nobody
added, and it makes every new member an edit to a shared file that the
next change's owned paths do not hold. A definition takes the new
member when it appears. So a check that must visit every application
component derives the components from the program that defines them,
and a rule that covers a class says what the class is. Where naming the
members helps a reader, they follow the definition as today's
instances, which go incomplete rather than false when the set grows. A
list stays only where membership is a decision nothing can derive, a
census, and then it is that census, declared once
([pulumi.md](pulumi.md), "Data: conventions, configuration, censuses").

## A workaround for an upstream defect is a layer of its own

Code that works around a defect upstream — in a provider, a tool, a
library — is built as one separable layer, designed as though upstream
were already fixed:

-   **Its interface is the one the architecture would have without the
    defect.** Callers ask for what they need, and the layer supplies it
    by whatever detour the defect forces. Nothing outside the layer takes
    a parameter, a field or a step that exists because of the defect.
-   **Its boundary is stated**, where the layer is written: which
    modules, steps or rules make it up, so that removing it means reading
    a list rather than investigating.
-   **Its removal condition is stated** beside the boundary — usually
    the upstream issue or release that fixes the defect, written as what
    is observably true once it has.
-   **A tripwire holds it** — a test, or a step of a CI job — red when
    the upstream behavior the layer answers changes: what the detour
    leans on moves, or the defect is fixed. The layer then neither
    breaks unnoticed nor outlives its reason. Where a version or a
    behavior can be observed by a step or a test of ours, the tripwire
    observes it there: a job step can read the version of a tool its
    runner carries and fail once that version ships the fix. Only for a
    party that nothing of ours can observe, such as a service hosted on
    the forge, is the removal condition written at the layer the whole
    guard, standing where whoever edits the layer is looking
    ([testing.md](testing.md) says why reach decides).

**The test of a good layer is the day upstream fixes the defect.**
Removing the workaround then means deleting that one layer, and no
interface or data shape elsewhere was bent for it. A detour threaded
through the code instead stays after the fix ships, because by then
nobody can tell which parts of an interface exist for the defect and
which for the design, and the detour has become the design.

The layers built this way today include the Renovate rule that looks
`postgresql` up under its channel, as `conda-forge/postgresql`
(`renovate.json5`), which goes once Renovate's mise manager passes a
pin's channel on; the section of `src/kluster/components/talos/__init__.py`
that hands what `Secrets` generated on as each consumer's input type,
which goes once an output type handed to an invoke reaches the provider
under the provider's names (pulumi/pulumi#15883); the mark
`src/kluster/components/monitoring/operator_updates.py` puts on each
VictoriaMetrics resource, a digest of what it is rendered from, which goes
once the operator takes a field a resource stops setting off its workload
by itself, the day the case of the defect in `tests/test_operator_updates.py`
turns red; and, in the test suite, `mocked_runs_release_their_tasks`
in `tests/conftest.py` and `_ReadFailures` in `tests/mock_monitor.py`,
which go once the Pulumi SDK stops leaving a closed loop's outputs in its
tracked set, and once its output awaits every future it was given; and the
section of `tests/conftest.py` that closes the event loops the SDK's mocks
give executor threads, which goes once the SDK closes those loops itself or
gives the threads none.

## Naming

**Descriptive over metaphorical.** A name states what the thing is, not
what it is like. No jargon-for-flavor when a plain compound says it:
container service beats estate, initial state beats seed. Rename
cost is paid once; decoding cost is paid by every reader.

**One term per concept, everywhere.** When two similar things coexist
(the ZeroTier overlay, the LAN networks), every class, method, and
variable says which one it means; a word that could mean either is
renamed until it cannot. The vocabulary this rule has produced is the
glossary in the `conventions` package's module docstring
(`src/kluster/conventions/__init__.py`) — each term, what it names,
where the tree uses it that way, and the words it refuses — which the
reviewer reads a diff's names against: a word a `Not:` line refuses, or
a second word for a concept that has an entry, is a finding, and a diff
that mints a term adds the entry with its pointer.
`tests/test_conventions.py` holds the package to it.

**One thing, one name.** An object keeps the name it already has,
wherever it is written about: the state backend is the state backend,
and its backup is its backup. No alias is coined for it unless one is
needed, and a new name, where one is needed, says what it names. Where
"one term per concept" keeps a word to one meaning, this keeps an
object to one word: a second name makes the reader ask whether there
are two objects, and a search for either name misses what was written
under the other. A name recorded outside the repository — a key name, a
unit, a URN — stays unless the change that renames it carries the
migration, and its declaration says so ([testing.md](testing.md), "A
name that state or the outside world keys on").

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

**A section is cited by the number its document visibly gives it.**
`§N.M` is a numbered heading or an inline `**§N.M**` label. A numbered
list item is `§N.M` only under a heading with no numbered heading or
label beneath it; elsewhere it is cited in words, "item M of §N", since
a reader who finds `### N.1` and `### N.2` but no `### N.3` reads `§N.3`
as stale rather than as the third item of a list. The sweep
(`tests/test_docs_sections.py`) holds this, so a citation of the other
kind fails the gate rather than reading as a stale number.

**A document that retires holds only what retires with it.** A runbook
for a move made once, or a table that lives as long as a transition,
says when it goes. A procedure in it that runs again after that day is
written somewhere that outlives it, or the retirement sentence names
where it moves; otherwise the procedure is deleted with the file, and
whatever pointed at it points at nothing.
[physical/gateway-cutover.md](../physical/gateway-cutover.md) is the
example: it describes the cutover window and the bring-up steps that
follow it once, and it is deleted when they have run. Replacing CI's
overlay identities, re-attaching a node volume and removing a DS recur
after it, so they are written in [credentials.md](../credentials.md)
§4.1, [declarative/physical.md](../declarative/physical.md) §6 and
[declarative/dns.md](../declarative/dns.md) §1.3, and its retirement
sentence names where each went.

**A command a runbook gives to be run as written, in a block, a
helper function or inline, reaches a pinned tool through `mise x --`;
an inline span that names a reading uses the tool's own spelling.** A runbook is
a procedure an operator runs against the estate: its cluster, its
hosts, its stacks and their services. The protocol an
agent works this repository by is not one, the `jj` forms of
[framework/dispatch.md](../framework/dispatch.md) and
[framework/testing.md](../framework/testing.md) among it, and a
recovery that must work without the checkout's pinned tools names its tool
bare: the one in `escrow/README.md` needs nothing but `age`. A pinned
tool is a command that `mise which` resolves, from the checkout, to one
of the pins in `mise.toml`: `jq` from its own pin, `psql` and
`pg_restore` from the `postgres` pin, `vmctl` from the VictoriaMetrics
one. A utility POSIX specifies is not one of them: the `gawk` pin puts
an `awk` on the path beside it, and a block that calls `awk` asks for
the host's POSIX one. The pin is what makes a reading the same on
every workstation: a bare name runs whichever release the host has,
or fails where it has none, and what it reads is then that host's
answer. A helper that wraps the tool, such as `kub` or `tal`, carries
`mise x --` once, and what runs through it needs nothing more; a filter
a block pipes into, such as `jq`, is a command of its own and carries
it too. The binary `mise which` names is the same pin, and any command
may call it; one that must not take the environment `mise.toml` sets
has to, such as a scratch probe with a backend of its own
([framework/testing.md](../framework/testing.md) §5.1). A command is
run as written where a step gives it whole for the operator to run,
placeholders and all, so an inline step carries `mise x --` as a block
does: `mise x -- kubectl -n monitoring get pods`. A span that names a
tool, a subcommand or a reading in prose, such as `talosctl upgrade` or
`pulumi stack init`, gives nothing to run, and keeps the tool's own
spelling. The rule is for commands run on
the workstation from the root of the checkout, where `mise.toml` is in
force, and a block or a step that reaches a pin says that it runs
there, beside the command or once for its section. Outside
the checkout, `mise x` runs whatever the path holds and says nothing,
while `mise which` refuses; so a filter on a reading taken in another
directory runs outside that directory's subshell. A command a block
runs on another host, the gateway or the state backend's box, runs that
host's own tool.

**A runbook reading whose passing value is empty or zero reads only
what its source returned. Its pass criterion says that a read which
prints no count is no reading. A baseline it is compared against is
shown when it is taken.** A filter at the end of a pipe, `grep -c` or
`wc -l` or a `jq` selection, prints that passing value on empty input,
and a source that failed hands it exactly that: the failure shows on
standard error alone, and the reading passes. So the source's output
is kept apart and filtered only when the source succeeded,
`log=$(…) && grep -c … <<<"$log"`, and a failed source prints its
error in place of the count. A baseline is read once and compared
against later, so a baseline taken from a failed read agrees with a
later reading that failed the same way; printing it where it is taken
is what shows it whole before anything is compared with it.

**Where a script runs a runbook, the runbook holds the meaning and
the script the commands.** The runbook states what each step changes
and why, what it reads and how each reading is judged, and what a
failure leaves. The script holds the commands, and says no more about a
step than its code needs, citing the runbook by step for the rest.
Neither restates the other, so a command changes in one place and a
judgment in the other. A seam test holds the two to one list of steps:
the runbook's step headings, read out of the document, are the script's
steps in order, and each heading names the command that runs its step.
A step added to either side alone fails it, and a step's prose reworded
does not. [operations.md](../operations.md) §3.5 is the instance, which
`drill rebuild` runs and `tests/test_drill.py` holds.

**Docs layer like the code.** `docs/framework/` documents mechanisms
(how this repo does Pulumi, CI, testing, and how work is dispatched)
and names no kluster design decision; `docs/declarative/`, `docs/physical/` and `docs/cluster/` own
the design; `docs/rfc/` holds the accepted proposals those documents
were changed by, as history rather than as reference; this directory
owns how things are written. What that leaves a vocabulary sweep free
to edit is [framework/rfc.md](../framework/rfc.md) §5.3.

### A count stands beside the members it counts

**A count is written only where the members it counts are in view** —
in the same paragraph, or in the list or the subsections immediately
below it. There the number is a reading aid the reader verifies on the
spot, and anyone who grows the list is already looking at it: a set
that grows is not the problem, distance is. Everywhere else the count
is a liability, and a set counted anywhere other than immediately above
its own members is named without a number — "a decision that moved
during construction", not "one decision moved". Dropping it turns a
sentence that goes **false** as the set grows into one that goes
**incomplete**, and incomplete fails open, because a reader who follows
it finds more than was promised instead of hunting for something that
is no longer there.

**A count in a comment is a claim about a list the reader cannot see**,
so the members it counts are never in view as the paragraph above means
it. Prose counts members that sit in the same document, where a reader
verifies the number on the spot. A comment counts a body of code
instead — declarations spread down a file, some of them conditional —
and nothing brings the two together: the comment is read where it
stands, the code changes where it stands, and a reader who disagrees
with the number has no way to tell which of them is wrong.
Name the members, or point at the list, and let the code be the census:
"the pinhole is the only conditional policy", not "ten rules, one
forward".

### A count that belongs to another document is never restated

**A count that belongs to another document is never restated**,
wherever that document keeps it and however close its own members sit
to it there. Its failure is not a stale count's: a stale count of
one's own set leaves the sentence incomplete, while a copied count can
lose its referent altogether — a reader sent to find "the two
criteria" in a document that now states three has nothing to match the
sentence against, and no way to tell which one went missing. Naming the
members as anchors is fine, since a list left short fails open the way
any incomplete sentence does; it is the quantifier that may not travel.
Cite the document and leave the number where the members are.

### A sweep looks for the claim, not the identifier

**When a change makes a claim false, sweep for the claim, not for the
identifier that moved.** Searching for the symbol finds call sites; the
sentence that asserted something about them is prose, and only a search
for the claim itself finds it. Five things keep that search from being
open-ended:

-   **Anchor the pattern on the claim's nouns, not on its predicates.**
    A predicate is where a document's voice varies — "comes from", "is
    read at", "lives in" — while the nouns of a technical claim are
    this repository's own fixed terms. A proximity match between two
    nouns, with no verb in it at all, is the form that survives
    whatever voice the target happens to be written in.
-   **The operator between those nouns is `.{0,N}`**, matched over text
    whose whitespace has been collapsed first, or under `re.S` where it
    has not:

        state backend.{0,120}B2

    `\W{0,N}` is the trap. `\W` matches a *single* non-word character,
    so the run spans punctuation and whitespace and **cannot cross an
    intervening word**: over `state backend exists. Mints the B2
    management key`, the pattern above matches and
    `state backend\W{0,120}B2` returns nothing. The weak form reads as
    "these two terms within a hundred and twenty characters of each
    other", which is what a proximity sweep means and not what it does.
    The naming rule below does not catch it either: a reader handed the
    pattern reads the intent rather than the defect. `\b` has the same
    shape of failure on a noun with an underscore in it: `_` is a word
    character, so `\brestore\b` does not match inside `pg_restore`, and
    a boundary meant to keep `restores` out keeps every `pg_` form out
    with it.
-   **Make a second pass for closure operators near those nouns** —
    "nowhere else", "only", "never", "no other", "and nothing else". A
    change that adds an exception leaves the affirmative form merely
    incomplete but falsifies the closed form outright, and the closed
    form shares almost no vocabulary with the affirmative one. The
    variant hardest to reach by matching predicates is the one whose
    staleness costs the most.
-   **A sweep that found nothing names the patterns it used**, in the
    pull request or the commit message. "It appears nowhere else in the
    repository" is not a result a reviewer can falsify.
-   **A pattern that has never fired is not evidence when it reports
    nothing, and it is exercised in both directions.** Before a clean
    sweep is read as an absence, run the pattern against a string that
    must match — the claim as some document actually words it — and
    confirm it fires; then against a passage the claim is absent from,
    and confirm it stays silent. The first direction separates "the
    claim is gone" from "the pattern could never reach it". The second
    is what makes a hit attributable to the claim rather than to a
    pattern that would fire anywhere: a mistyped noun leaves `.{0,120}`
    to do the work, passes the positive check on its own, and turns the
    sweep into a page of hits to read by eye — a degradation nothing
    else in the run announces.

**Every pass is newline-insensitive.** Prose here is soft-wrapped, so
the break falls wherever the wrap puts it, and the line a line-oriented
matcher sees holds only half the claim: the match returns nothing and
the sweep reports clean. Collapse whitespace before matching — a few
lines of Python over the files, which does the proximity matching above
in the same pass — or run a matcher in a mode that is not line-oriented
where it has one, or search for the rarest single word in the phrase
and read the hits. Under-reporting is the failure mode that matters
here, because it is silent and looks exactly like a clean sweep.

**Every pass reports how many files it read, and a skip set is tested
against paths relative to the tree being swept.** A sweep run inside a
workspace stands under the primary checkout's `.claude/`
([framework/dispatch.md](../framework/dispatch.md) §1.2), so a skip set
written to keep scratch out excludes every file when it is tested
against absolute paths, and the sweep visits nothing and reports clean.
The file count is the control that catches it, the way the positive
match above catches a pattern that cannot fire: a sweep that read no
files has found nothing about the repository.

**Scratch is what `.gitignore` ignores, not everything under
`.claude/`.** The role definitions in `.claude/agents/` and the project
settings in `.claude/settings.json` are tracked, and a sweep reads them
like any other file; everything else under `.claude/` — workspaces,
worktrees, tool configuration, dumps — is ignored scratch. So a skip set
never names `.claude` itself: it names the ignored paths, which are
`/.claude/*` less the paths `.gitignore` re-includes after it. The walk
that needs no skip set at all is the tracked files — `jj file list`
inside a workspace, `git ls-files` in a checkout — which hold no scratch
to begin with.

### A rule a model gate quotes holds its heading alone

**Each rule a model gate quotes stands under a heading of its own,
which holds that rule and nothing else.** A gate quotes its rule as a
section of the document that states it, read from the tree at the head
under review ([framework/dispatch.md](../framework/dispatch.md) §3), and
that section's text is part of what the gate's calibration measured. An
edit anywhere in the section stales the gate, which then runs in shadow
until it is measured again, so a second rule sharing the heading stales
it on every edit to that other rule. It also puts two questions to a
gate measured on one: a miss cannot be laid on either rule, and the
measurement says nothing about either. A rule written beside one a gate
quotes takes a heading of its own.

## Review gate

Every pull request passes an independent review against these rules
before merge; how that review is run and by whom is
[framework/dispatch.md](../framework/dispatch.md) §3. Major structural
changes go the other way around: an RFC in [`docs/rfc/`](../rfc/)
states the desired end state, names the design documents its content
must land in once built, and is approved before implementation starts.
