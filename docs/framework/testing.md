# Testing

Objective: Verify infrastructure logic without creating real cloud resources
using Pulumi mocks and `pytest`, and rehearse against the real providers
where no mock can answer the question.

The suite has three tiers, and each one is bounded by what it can know:

-   **Pulumi unit tests** (§2, §3) check the shape of what a component
    registers, against `pulumi.runtime` mocks.
-   **Fakes** (§4) stand in for an external service — a tenancy, a store — in
    the tests of the code that drives it. A fake carries the service's
    authorization and failure semantics, not only its happy path, and the set
    of behaviors it carries only grows.
-   **Live drills** (§5) run against a real account. They exist for the class
    of defect the other two tiers structurally cannot reach: our assumptions
    about the provider being wrong.

## 1. Setup

The project uses `pytest` and `pytest-asyncio` for unit testing.

To run the tests (always with a timeout — a coroutine that never resolves its
futures hangs forever instead of failing):

```bash
timeout 60 mise x uv -- uv run pytest
```

### 1.1 A test process holds no credentials

`mise.toml` materializes the operator's account-root token into
`GITHUB_TOKEN`, the state passphrase into `PULUMI_CONFIG_PASSPHRASE` and the
backend URL into `PULUMI_BACKEND_URL`, out of files rather than out of the
caller — so each wins over anything set on the command line, and a `pytest`
run started the way above carries live credentials whether the suite wants
them or not. That matters because **Pulumi prints a resource's inputs when an
assertion about it fails, and a provider's inputs include its credential**:
the first failing assertion in a suite that declares a provider renders
whatever the environment was holding into the report.

`tests/root_credentials.py` is the whole mechanism, and `tests/conftest.py`
applies it at import. Deliberately not through a fixture, not even one every
suite gets without asking: the earliest a fixture can run is the setup of the
first test, by which point every test module has been imported, so a suite
that read a variable while being collected would still have seen the
operator's value.

-   **What is masked** is every variable an account root can arrive in, read
    off `masters.ROOTS` rather than listed a second time, plus the two
    backend secrets above. A root added to that register is masked by that
    addition alone.
-   **A suite that needs a value asks for a fake one by name**, with
    `root_credentials.fake_credentials('GITHUB_TOKEN')` as a context manager,
    or `root_credentials.fake(name)` for the value on its own. The value is
    derived from the variable, so a value that does reach a diff identifies
    what it stood in for and says that it opens nothing. A name that carries
    no masked credential is refused, because setting one would be relying on
    a protection that is not there.
-   **A suite that needs one and does not ask** meets whatever the code under
    test raises for an unset variable. `kluster.stacks.github` is the worked
    example: it refuses by name rather than authenticating as nobody, so the
    failure says which credential was missing.

The masking covers the live tier too (§5). A drill reads its credentials from
the kit, never from the ambient environment.

## 2. Writing Tests

A suite that declares resources starts from `tests/mock_monitor.py`, which
carries the three pieces every such suite needs. It is a named module rather
than a `conftest`, because test modules import it and `conftest` is not a name
an import can aim at.

-   `Recorder` is a `pulumi.runtime.Mocks` that invents nothing: it answers
    each registration with the resource's own inputs and an id built from its
    logical name, and remembers every declaration -- its type, its inputs and
    the provider instance it was registered against.
-   `run_with` points the runtime at a monitor and hands it back. It also
    primes the parameterization feature a bridged provider reads before it may
    register anything, and empties the registration queue left behind by
    whichever run went before.
-   `declaring` is the barrier. Declaring a resource only schedules its
    registration, so without it the monitor has seen nothing and every
    assertion about it passes vacuously.

What a suite writes for itself is only the part that is its subject: an output
the provider computes that the inputs do not carry, and the answer to an
invoke.

### 2.1 Example Test

Create a file named `test_*.py` (e.g., `test_network.py`) in the `tests`
directory.

```python
from typing import Any

import pulumi
import pytest
import pytest_asyncio
from mock_monitor import Recorder, declaring, run_with

from kluster.components.cloud import CloudNetwork


class Cloud(Recorder):
    """The one answer this suite is about: the prefix the account assigns."""

    def computed(self, args: pulumi.runtime.MockResourceArgs) -> dict[str, Any]:
        if args.typ == 'oci:Core/vcn:Vcn':
            return {'ipv6cidrBlocks': ['2001:db8::/56']}
        return {}


# Must be an async fixture: `set_mocks` needs the test's running event loop.
@pytest_asyncio.fixture(autouse=True)
async def monitor() -> Cloud:
    return await run_with(Cloud(), stack='physical')


@pytest.mark.asyncio
async def test_the_subnet_is_carved_from_the_assigned_prefix(monitor: Cloud) -> None:
    async with declaring():
        network = CloudNetwork('test-vpc', compartment_id='ocid1.compartment.test')

    assert await network.subnet.ipv6cidr_block.future() == '2001:db8::/64'
    assert monitor.names('oci:Core/subnet:Subnet') == {'test-vpc-subnet'}
```

A case that reads the component's own outputs needs no barrier -- awaiting an
output registers what it depends on. `declaring` is for the cases that ask the
monitor what the run registered.

## 3. Testing Next-Gen Components (RFC-001)

When writing unit tests for components using the `putils.Component` base class
with `async_output`/`resolve` inputs:

### 3.1 What the mock monitor drops

Two things a case may want are not reachable through Pulumi's own mocks, and
`tests/mock_monitor.py` recovers both by patching `MockMonitor.RegisterResource`
once, at import, before any Pulumi code runs:

-   **`propertyDependencies`.** During registration the engine is told which
    outputs a resource property depends on; the mock monitor's response drops
    them, so a dependency assertion would always come back empty. The patch
    copies them from the request onto the response.
-   **The resource options.** `import`, `ignoreChanges`, `deleteBeforeReplace`
    and `dependsOn` reach no output at all. The patch keeps each registration
    request on the `Recorder` the run was built around, where
    `Recorder.options_of(name)` and `Recorder.depends_on(name)` read them.

The patch belongs in one place because it does not compose: a second module
capturing "the original" at its own import time would chain onto this one, and
which chained onto which would be decided by collection order.

### 3.2 Asserting Dependencies via URNs

When Pulumi tracks dynamic dependencies inside `async_output` coroutines, it
recreates dependency instances as `DependencyResource` synthetic resources.
**Do not use object identity (`is` or `==`)** to compare resource dependencies
returned by `Output.resources()`. Instead, extract and assert on their `urn`
strings. Note that `resolve` cannot be used in test code — it raises
`RuntimeError` outside an `async_output` coroutine (RFC-001 Rev 3); await
output futures directly:

```python
# Correct assertion pattern:
deps = await my_component.subnet.network_id.resources()
dep_urns = {await d.urn.future() for d in deps}
assert await vpc.urn.future() in dep_urns
```

### 3.3 Dry-Run/Preview Safety

In dry-run tests (where `preview=True`), unresolved output properties return the
`UNKNOWN` sentinel. Awaiting such outputs via `resolve` raises
`UnknownValueException`, which `async_output` converts into an unknown output.
If testing preview behavior, retrieve the output's future using
`.future(with_unknowns=True)` to inspect if it's an instance of `Unknown`:

```python
from pulumi.output import Unknown

val = await my_component.subnet.network_id.future(with_unknowns=True)
assert isinstance(val, Unknown)
```

## 4. Fakes and the Ratchet

Code that drives an external service is tested against a fake of that service
(`FakeIdentity` in `tests/test_oci_iam.py` is the worked example). A fake is
not a stub that returns success; it is the smallest model of the service that
can still tell a correct caller from an incorrect one.

**A fake carries authorization semantics.** It records which principal made
each call, so a test can assert *who* did something and not merely that it
happened. Whether the sweep of superseded API keys runs as the seed user or as
the account root is the whole of one defect, and it is only visible because
the fake tenancy remembers the identity behind every connection.

**A fake carries failure semantics.** It refuses what the real service is
known to refuse: the three-key quota, a user created without a primary email,
a key that does not authenticate for the first few seconds of its life, an
identity layer that will not delete a key at all. Each of these is a subclass
of the fake or a guard inside it, and each exists because the real service
did it once.

**Every live failure teaches the fake.** When a run against a real provider
fails in a way the fake would have allowed, the fix has two halves that land
together: the new behavior is added to the fake, and a regression test
asserts what the code now does about it. The fake change comes before or with
the code change, never after — the point is that the test fails without the
fix.

**The ratchet only tightens.** A behavior, once a fake has learned it, is
never removed or weakened to make a test pass. A test that fails against a
stricter fake is reporting either a defect or an assumption that has to move
into the code under test; deleting the behavior deletes the only record we
have of what the provider actually does. Making a fake *more* faithful is
always allowed, and is how this tier improves.

**What this tier cannot do.** A fake encodes our assumptions about a service.
It can prove the code is consistent with those assumptions; it cannot prove
the assumptions are right, and it never fails for a behavior nobody has met
yet. That class of defect belongs to §5, whose purpose is not to prevent it
but to relocate it out of an operator's bring-up and into a rehearsal.

## 5. Live Drills

`tests/live/` holds drills that talk to real accounts with real credentials
and change real state. They are not collected at all unless the opt-in is
present:

```bash
RUN_LIVE_DRILLS=1 timeout 600 mise x uv -- uv run pytest tests/live -s --log-cli-level=INFO
```

`tests/live/conftest.py` is the entire mechanism: without `RUN_LIVE_DRILLS=1`
it declines to collect the directory, so an ordinary `pytest` run neither
executes a drill nor reports one as skipped. There is no marker and no
`addopts` entry to keep in sync.

A drill reads its credentials from the same store the command-line entry point
uses — for the credential drills, `KdbxStore.from_env` on `$KLUSTER_KDBX`,
unlocked from the desktop secret store. It cannot read them from the ambient
environment: §1.1 masks those for the whole process, the live tier included.
Run `credentials kit password remember` first, or pass `-s`, so the prompt
reaches a terminal.
`--log-cli-level=INFO` is what makes the run a transcript worth pasting.

Two properties are required of every drill, because an operator has to be
able to run one on a whim:

-   **Idempotent**: it ends in a state it can start from, so running it twice
    in a row is the same as running it once.
-   **Safe to repeat**: it performs the operation it is drilling, not a
    destructive approximation of it, and it leaves no credential behind that
    it did not clean up (or, where the provider refuses the cleanup, it says
    so).

**When a drill is required.** A change to provider-facing code ships with a
drill transcript in the pull request, or with an explicit "unproven live"
note saying what the first live run must confirm. "Unproven live" is a
legitimate state — an agent without credentials cannot do better — but it is
stated, not assumed, and the claim is settled by the next person who has a
terminal and an account.

**Current drills.** `tests/live/test_oci_seed_drill.py` rotates the OCI seed
key twice against the real tenancy, asserting after each rotation that exactly
one usable key stands: the key the kit holds is the key that authenticates,
and a surviving second key is proved to be a deletion the tenancy refused
rather than an orphan the sweep missed.

## 6. Proving a Test Fails Without the Change

AGENTS.md requires that new behavior ship with a test that fails without
it. That is a claim about the test, and the only thing that establishes
it is breaking the behavior and watching the test go red. The broken
version — the mutation — is throwaway code, and the whole of the
discipline below is about getting rid of it again without taking the
real change with it.

**Keep a pristine copy and mutate in place.** The throwaway artifact is
the copy, not the mutation:

```bash
cp <file> "$(mktemp -d)/orig"   # mutate <file> in place, run the suite,
                                # then copy the pristine one back
```

Mutating a copy beside the original and running the suite against *that*
does not work: the suite imports the package from the import path, so
the run is against unmutated code and passes — which in a mutation round
is the alarming answer, and reads as "the new test does not bite".

**The other correct answer is to commit first.** Describe the change and
`jj new`, so that the work sits at `@-` and the mutation is alone in
`@`. **`jj new` is the stash here**: `jj` has no stash of its own, and
`git stash` inside a workspace reports on the *primary's* tree — `No
local changes to save`, exit 0, the workspace's file untouched and its
real change still sitting in it, which is the setup for losing it.

Only after that `jj new` is `jj restore <file>` a revert of the mutation
alone; run against a `@` that holds the real change as well, it takes
both and exits 0. It is the recoverable failure, though — the working
copy is a commit, so `jj undo` puts back what the restore took.

**`git checkout HEAD -- <file>` is the trap, and it does not spring
where a builder here would expect.** In a checkout git can see — the
colocated primary, or a plain clone — it exits 0 and discards the real
change along with the mutation, with no prompt. What makes that hard to
catch is the state it leaves behind: the file stops appearing as
modified and `git status --porcelain` comes back empty, which reads as
*committed* rather than *gone*. There is no undo.

Inside a `jj` workspace the same command is instead **inert**. The
workspace has no git repository of its own, so git walks up to the
colocated primary ([dispatch.md](dispatch.md) §1.2) — and that walk
re-roots the `pathspec` as well as `HEAD`. `f.txt` resolves to
`.claude/workspaces/<name>/f.txt`, which is inside an ignored directory
and in no tree git knows, so the command fails with `error: pathspec
'f.txt' did not match any file(s) known to git`, exits 1, and changes
nothing. Read that exit 1 as "nothing happened", not as evidence that
something was lost.

**After any mutation round, verify the diff against the branch's own
base:**

```bash
jj diff --from main
```

Without `--stat`. The summary is decisive only for a whole-change loss
(`0 files changed`); after a partial one it reads `1 file changed, 1
insertion(+)`, which helps only a reader who remembers it should have
been two. The full diff shows both halves of the question — that the
change is intact, and that the mutation is gone. Nothing else does: a
test proved against a fix that is no longer there passes for the wrong
reason, so the suite stays green, the documentation still describes the
fix, and the diff is the only artifact that disagrees.

## 7. Best Practices

1.  **Mock Early**: Always set mocks before importing or executing any Pulumi
    code that creates resources.
2.  **Test Outputs**: Await `.future()` (or use `.apply()` /
    `pulumi.Output.all()`) to check values of outputs, as they resolve
    asynchronously; `resolve` is only usable inside `async_output` coroutines.
3.  **Always Use Timers**: When running unit tests, wrap the execution with a
    timeout (e.g. `timeout 15` in shell or using a test runner timeout) to
    prevent hanging tests when coroutines fail to resolve their futures.
4.  **Keep it Fast**: Unit tests should not make network calls or create real
    resources.
