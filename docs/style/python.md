# Python Readability

General Python style for everything in this repository — stack
programs, scripts, tests alike.

## Baseline: the Google Python Style Guide

The [Google Python Style Guide](https://google.github.io/styleguide/pyguide.html)
applies where it applies; where it conflicts with this repository's
rules, **this repository wins**. The known deltas:

-   Formatting and import order are `ruff`'s, not `pyink`/`isort`'s;
    line length and quote style come from `pyproject.toml`.
-   Type annotations are not "encouraged", they are required:
    `basedpyright` strict passes clean on `src/`, `tests/` and
    `deploy/` (AGENTS.md has the enforced relaxations).
-   Docstrings follow the Google shape but the comment discipline of
    [README.md](README.md): contract only, no narration.

## Repository rules

**One idiom per job inside a unit.** A function that assembles a list
picks one way to do it (comprehension, `append`, `extend`, unpacking)
and uses it throughout; mixing forms makes the reader hunt for a
difference that is not there.

**Data shapes are honest.** A parameter that must contain specific
keys is not a `Mapping` — it is a `dataclass`, a `TypedDict`, or
separate parameters, so the requirement is in the signature instead of
in the body's lookups. A function that ignores part of what it accepts
takes less.

**Validation happens at the boundary, loudly and by name.** Input
crosses from untyped to typed in one place, and a shape mistake is
reported as "what is wrong with which entry", never as a downstream
traceback.

**A design document naming `pkg.module.NAME` names the home, not the
call site.** A table in an RFC or a design document says where a value
lives, in the same voice as the row beside it naming a configuration
key; it is not telling a caller how to spell the import. How a package
is imported from is the package's own statement, made in its
`__init__.py` — which of its surface is re-exported flat, and which
domains are read qualified — and nothing outside the package overrides
it. `conventions.providers.CLOUDFLARE_ACCOUNT` in a design table
therefore says the constant lives in the `providers` module of
`conventions`; whether a caller writes it qualified or flat is answered
by `conventions/__init__.py` and by nothing else.

**Long literals are not code.** Another program's configuration
language (a config file, a rules program) lives in a file beside the
module, loaded by the shared mechanism — string literals in Python are
for names and one-liners the code owns.

## Tests

A test case reads top to bottom as: irrelevant setup out of sight
(shared fixtures and builders), the setup that *is* the case, the act,
the assert. Rules that keep it that way:

-   **Shared machinery is shared.** Mocks, fakes and builders used by
    more than one module live in one place (`tests/` helpers,
    `conftest.py`), not re-grown per file; a fixture used by one module
    stays private to it.
-   **A case states only what it tests.** Setup that any case needs,
    but no case is *about*, belongs in a fixture; if a reader cannot tell
    which line the test is about, the case is doing too much.
-   **The name is the sentence.** `test_<the claim being proven>`, and
    the body proves exactly that claim — one behavior per case.
-   **Not too DRY.** Tests have no tests, so their correctness must be
    self-evident: no fixture where none is needed, literal values over
    helper functions where a literal is readable. This complements
    "irrelevant setup out of sight" rather than competing with it —
    abstract the setup that is *not* the point into clean modules with
    clean interfaces, and keep the values that *are* the point literal
    and in view.

**A claim belongs at import time when the collection itself depends on
it** — a value that sizes a parametrization, or a property that must
hold before any case is meaningful. Those have no case to live in: the
collection they protect happens first, and a parametrization that
collects short still reports green, so nothing that runs later catches
it.

**Everything else belongs in a case, and the cost that decides it is
blast radius.** An assertion that fails during collection names a
*module* rather than a claim, and takes down every unrelated case in the
run — sibling modules importing the same table included — so whoever
added one entry to a table gets no results at all instead of one run
showing everything left to fix. A case failing beside its passing
neighbors costs one line of output; a collection error costs the run.

**An import-time assertion's message is all its author sees.** No case
name frames it and no fixture output accompanies it, so the message
carries the set difference — which entries are missing, which are
unexpected — rather than a bare truth value.
