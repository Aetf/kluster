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

A drill reads its credentials through the same environment and store the
command-line entry point uses — for the credential drills, `KdbxStore.from_env`
on `$KLUSTER_KDBX`, unlocked from the desktop secret store. Run `credentials
kit password remember` first, or pass `-s`, so the prompt reaches a terminal.
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

**A mutation belongs in a throwaway copy of the file, not in the working
tree that holds the change being proved.** Copy the file aside, mutate
the copy, run the suite against it, delete it. Nothing then has to be
reverted, and the step every loss of work turns on does not happen.

**Where the mutation has to be in place, commit first — or stash the
real work.** Describe the change and `jj new`, so that the work sits at
`@-` and the mutation is alone in `@`. A revert can then restore only
what the mutation changed, because there is nothing else in the file to
restore.

**`git checkout HEAD -- <file>` is the trap.** It is the obvious revert,
and it is destructive: on a file that also carries uncommitted work it
discards that work along with the mutation, without a prompt and without
a non-zero exit. What makes it hard to catch is the state it leaves
behind — the file stops appearing as modified, which reads as
*committed* rather than *gone*. A clean `git status` is evidence of
nothing. Inside a `jj` workspace it is worse, because the workspace has
no git repository of its own and `HEAD` answers about the primary
checkout ([dispatch.md](dispatch.md) §1.2): the file is restored to
whatever `main` holds, however many commits the branch is ahead. The
`jj` revert that means what it says is `jj restore <file>`, which
restores from the working copy's own parent.

**After any mutation round, verify the diff against the branch's own
base:**

```bash
jj diff --from main --stat
```

The change is either still there or it is not, and this is the check
that distinguishes the two. Nothing else does. A test proved against a
fix that no longer exists passes for the wrong reason, so the suite
stays green, the documentation still describes the fix, and the diff is
the only artifact that disagrees.

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
