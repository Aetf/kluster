# RFC 009: The Model Gates

*   **Status:** Accepted, 2026-11-26. A promoted design of 2026-11-21.
    Its harness and gates are on `main`, each in shadow; §10 says which
    of their parts this design keeps and which it retires.
*   **Created:** 2026-11-26
*   **Authority:** AGENTS.md,
    [framework/dispatch.md](../framework/dispatch.md) and `docs/style/`.
    A rule proposed beyond them is marked **new rule**; there is one
    (§4).
*   **In scope:** what a gate is for, is asked and over what, how it is
    measured, improved and made blocking, the key, the brief gates.
*   **Out of scope:** the deterministic checks and the rest of the
    `dispatch` script and its hooks (dispatch.md §2.1 and §2.2); the
    review's own form (dispatch.md §3); the wording of each rule a gate
    quotes, its document's own.

## 1. Context and problem statement

Reviews enforce the style rules and the dispatch protocol at the most
expensive point, and miss a rule the reviewer's context does not hold.
A deterministic check owns a rule's mechanical part; a model gate puts
the semantic remainder — is this assertion a change detector — to a
small model before a reviewer is commissioned.

The gates on `main` narrow a change to items through an extractor of
their own, and are measured on labelled items. Five are measured, and
none comes near the bar; a larger model does no better; more context in
the items moves recall little and adds false positives; and the misses'
reasons dispute the labels, which an item drawn by code and judged out
of its file cannot settle. So the model is given the change as a
reviewer reads it, measured against what reviews found, and taught from
that same history.

## 2. What is inherited, and what is decided here

| Inherited | Decided here |
| --- | --- |
| Every pull request merges after an independent review (dispatch.md §3) | A gate adds findings ahead of the review and replaces none of it (§3) |
| A rule is stated once, in its canonical document | A gate quotes its rule from the tree at the head (§4) |
| A reviewer tags a finding with the gate whose rule it breaks (dispatch.md §3) | Those tags are a gate's answers and its measurement (§6) |

## 3. What a gate is for

**A gate does not replace the review**, whose angles both still run. A
gate finding is a fix cycle the builder takes before a tech lead is
spent on it, at two orders of magnitude less cost; a rule only a large
model answers reliably stays with the reviewer.

## 4. One gate, one rule, quoted

A gate asked about two rules is measured on neither, since a miss cannot
be laid on one. Its rule is a section of a document in this repository,
read from the tree at the head under review, so the gate never restates
it. **New rule**, for [style/README.md](../style/README.md): each rule a
gate quotes stands under a heading of its own, which holds that rule
alone, since an edit to it stales the gate.

## 5. What a gate is asked, and over what

A gate sees the pull request's diff and the whole of each changed file
its trigger matches, with its rule's section, and is asked which changes
break that rule: each finding a place — a path and lines at the head, or
lines of a commit's message — with a reason. The model reads what a
reviewer reads first, and no code of the gate's own decides what the
question is about. The input is split only where it runs past a call's
bound: by file, and a file too long alone into runs of lines with the
hunks they hold.

**Three samples, two to stand.** Each call is asked three times, and a
finding stands where two samples' findings match (§6). Two runs of one
prompt part by as much as a prompt change moves the score, so one sample
cannot tell a better prompt from a lucky one; the gates cost cents.

## 6. Measured per pull request, against past reviews

A gate's history is its cases, and every disagreement accrues to it:
each finding a review tagged with the gate at a head it read, and each
gate finding a reviewer, the dispatcher or the operator ruled on. A case
is a place, a verdict and the comment it came from, and holds no text:
the head is in the forge, and the change is the head against its merge
base with `main`.

**A gate finding matches a review finding** when both name one path at
the same head and their lines lie within a margin set beside the bar,
one to one. A model and a reviewer point at one defect a line or two
apart, and exact lines would score that as a miss and a false finding.

**A finding no review made is confirmed once**, with a tag beside its
place (dispatch.md §3) that binds to that place and head for every later
run. The reviewer tags those at the head it reads. `calibrate` lists
each finding of the run it measured that stands unconfirmed — a replay
on reviews older than the gate, or a finding fixed at a head no reviewer
read — and writes no record while one does; the dispatcher tags them in
the calibration pull request. The operator's tag replaces any other.

**The score** is the review findings recovered over the whole history,
and the false findings per pull request the gate ran on. The bar of
`gates.calibration` starts at four in five recovered and one false
finding in five pull requests, over at least eight findings to recover
and twenty pull requests. A false blocking
finding costs an appeal and a ruling, so the bar counts what a pull
request pays.

**The history lives in this repository**, as each gate's `cases.jsonl`
of places, verdicts and links to the comments they came from. A case's
place is at a head of this repository, and its source may be any
comment, an operator's ruling in a private repository included, since a
case holds a link and no text. A brief, whose place is at no head here,
makes no case (§14). A head the forge no longer serves drops its cases,
and the report names them.

## 7. Improving continuously

**The most relevant past cases are the prompt's examples**, up to a
fixed budget, chosen deterministically: the cases whose path shares the
most leading directories with a file the run sees, then the most recent,
breaking and clean in turn, each with its lines and the reason its
comment gives. An example shows a finding in the place it occurs, which
a definition alone does not. **An example is never scored**: a pull
request is measured with examples drawn from every other pull request's
cases, so no case is scored by a prompt that holds it.

**A change to a prompt or a model lands only if it does no worse on the
whole history**: `calibrate` measures it beside the current gate in one
run, and writes the record only when it recovers no fewer findings and
carries no more false ones per pull request.

## 8. The record and what makes a gate blocking

**A gate is shadow until measured.** Its record, `calibration.json`,
holds the score and the hashes of the prompt, the canonical section, the
example selection and the model; a change to any of them stales it, and
it then reads as shadow and names the part that moved. A case added
stales nothing: it moves the next measurement.

A gate blocks a pull request while its record is current and meets the
bar as it stands. **The model is the smallest that meets the bar**: a
gate starts on `gate.DEFAULT_MODEL` and moves up a size only through the
regression check.

## 9. The key and where the gates run

The gates run on the dispatcher's workstation with the model gates' key
([credentials.md](../credentials.md) §3), and no CI job runs one: the
briefs are in a repository CI cannot read, a required check
([github.md](../framework/github.md) §3) runs on a bot's pull request
too, and a pull request's runs gain no credential.

## 10. What is already conformant

The built parts this design keeps: a gate's directory, `prompt.md`,
canon and trigger; the record, its staleness and the test holding it;
`calibrate`'s per-case report and its refusal on an unanswered sample;
`harvest`, `appeal`, the tags and the findings comment; three samples;
the brief gates at claim; the key. Slice 2 retires the rest: each
extractor, `extract`'s items, the passages of `gates.reading` and
`gates.prose`, `ask`'s item batches, a case's text, the falsifier pair,
and `falsified-claim`, whose question is about files a change does not
touch and needs a sweep of the tree, an extractor by another name; its
rule stays with the reviewer.

## 11. What does not propagate

*   **The review's severity.** A gate's finding blocks by the status its
    measurement earned, never by the severity a review gave its cases.
*   **Reading the repository.** A gate reads the change and its
    examples; a file the change does not write is the reviewer's.

## 12. The documents this content lands in

| Document | What lands there |
| --- | --- |
| [framework/dispatch.md](../framework/dispatch.md) §2.1 and §3 | The run, the measurement, the improvement; the brief verdicts advisory, which reverses its refusal of a claim (§14) |
| [style/README.md](../style/README.md) | §4's new rule |
| The package documentation of `src/kluster/scripts/dispatch/gates/` | A gate's parts, the bar, the example selection |
| [credentials.md](../credentials.md) §3 | The key. Landed |

## 13. How we get there

Every gate stays in shadow until slice 4, so nothing half-built blocks a
review.

1.  **The whole-file ask.** `dispatch gates` asks each gate as §5 says,
    and `dispatch claim` asks about the brief whole, its verdicts
    blocking nothing as §14 decides; dispatch.md §2.1 and §3 say so.
    Done when a run on a recorded change posts findings by line and
    calls no extractor, and no verdict refuses a claim.
2.  **Cases as places, scored per pull request.** Today's cases convert
    to places, `harvest` records places, `calibrate` scores as §6 says,
    the bar moves to §6's, and what §10 retires is deleted. dispatch.md
    §3 states the measurement, and style/README.md the new rule of §4.
    Done when every gate's history measures from places alone.
3.  **Examples and the regression check**, as §7 says. Done when no
    pull request is scored by a prompt holding its own cases, and
    `calibrate` writes no record that does worse on the history.
4.  **A calibration per gate**, by the dispatcher, a slice for each
    gate after slices 2 and 3. Done when its pull request carries the
    gate's record, at the bar or short of it.

## 14. A verdict on a brief is advisory

A gate's verdict on a brief lands in the claim's log and blocks
nothing, whatever status the gate's calibration earns. A fault in a
brief recurs in every line built from it, but no history holds a brief
(§6), so a status measured on pull requests says nothing about how the
gate reads one, and the brief is rewritten before the agent is spawned
whichever way the verdict reads. This reverses dispatch.md §2.1, where
a gate whose calibration earns `blocking` refuses the claim; §12 names
the change to that rule, and slice 1 makes it.

The alternative, briefs gaining cases harvested from the claim's log
into a history kept outside this repository, loses: it keeps a second
history beside the one §6 places at heads of this repository, to
measure an input the dispatcher rewrites before anything is built from
it.

No other question in this design is left open.
