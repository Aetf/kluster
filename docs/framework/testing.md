# Testing

Objective: Verify infrastructure logic without creating real cloud resources
using Pulumi mocks and `pytest`, and rehearse against the real providers
where no mock can answer the question.

The suite has these tiers, and each one is bounded by what it can know:

-   **Pulumi unit tests** (§2, §3) check the shape of what a component
    registers, against `pulumi.runtime` mocks.
-   **Fakes** (§4) stand in for an external service — a tenancy, a store — in
    the tests of the code that drives it. A fake carries the service's
    authorization and failure semantics, not only its happy path, and the set
    of behaviors it carries only grows.
-   **Engine tests** (§8) run the pinned `pulumi` against a program of
    stand-ins, for what the engine itself does that a design depends on —
    around a hook, a replacement, an ignored input — which no mock can show.
-   **Live drills** (§5) run against a real account. They exist for the class
    of defect the other tiers structurally cannot reach: our assumptions
    about the provider being wrong.

## 1. Setup

The project uses `pytest` and `pytest-asyncio` for unit testing.

To run the tests:

```bash
timeout 1200 mise x uv -- uv run pytest
```

The run is spread over four worker processes by `pytest-xdist`, through
`addopts` in `pyproject.toml`, so a workstation's gate and CI's run the
same way. Four because the CI runner has four cores. The count is fixed
rather than `auto` because a workstation here runs several agents' gates at
once, and `auto` would start a worker per core for each of them. So a case
shares the machine with three others of the suite's own, and holds only
state that is its own: its `tmp_path`, its backend and its `PULUMI_HOME`, a
container named for the case. A run aimed at one case passes `-n 0`, which
runs it in the `pytest` process itself. `addopts` also passes `-rfEs`:
the summary `pytest` gives by default, failures and errors, with every
skipped case and its reason added. `tests/test_state_roles.py` skips
where the Postgres image it runs is not local, and runs on CI as it does
on a workstation, because `checks` pulls that image before the tests
([ci.md](ci.md) §3).

A coroutine that never resolves its futures hangs forever instead of failing,
and the run is bounded twice against that, at two scales:

-   **Per case**, by `pytest-timeout`, configured in `pyproject.toml`
    (`[tool.pytest.ini_options]`). A case that hangs fails by name, with the
    stack it hung in — a frame waiting on a future is a hang, a frame doing
    work is a stalled machine — and the run goes on to a summary, so one
    hung case costs one red line rather than the whole report. The signal
    method is the configured one because it is what lets the run continue;
    the thread method ends the process after a stack dump, with no summary,
    which is the same blind result as a kill from outside. A worker runs its
    cases on its main thread, so the signal reaches them there as it does in
    a run without workers, and the worker goes on to its next case. The
    bound is a hang guard, an order of magnitude above every case it
    bounds: under the four workers the slowest take about 4 s. On a
    machine loaded four times over -- twelve busy processes beside the
    workers on four cores -- the slowest take 8–12 s, five times under it.
    A case that starts the pinned `pulumi` CLI carries a bound of its own
    instead, set from measurement, because its duration grows with the
    machine's load (§8).
-   **Around the run**, by the outer `timeout` above, for what a per-case
    bound cannot reach: collection, and a process still alive after the
    summary is printed. A kill there ends with status 124 and no summary,
    which is why it is an order of magnitude above the run's duration
    rather than a budget the ordinary run approaches — the suite takes
    about two minutes on four cores, and it grows with every campaign.

The outer number is carried wherever the gate's command is written out,
and the per-case number lives in `pyproject.toml`, apart from the
real-CLI cases' own, which live beside them (§8).
`tests/test_gate_command.py` holds every launch of `pytest` it finds to one
form, timed and through `mise`, and every launch of the whole suite to one
number. What CI executes it finds by definition: every launch it knows
the spelling of — `pytest` or `py.test` as a word or at the end of a path,
or `-m pytest` — in any workflow or local action, today the `Tests` step
of `checks.yml` and of `sdk-regenerate.yml`. The prose it reads is a
named set of documents, each read whole — AGENTS.md, README.md and this
one, the ones that write the command out. Any other document points at
one of those rather than writing its own. A run aimed at part of the
suite, such as a drill's (§5), is not the gate and carries its own number.

### 1.1 A test process holds no credentials in its environment

The Pulumi stack passphrase and the backend URL are materialized by
`mise.toml`, into `PULUMI_CONFIG_PASSPHRASE` and `PULUMI_BACKEND_URL`, out
of files rather than out of the caller — so each wins over anything set on the
command line — and an operator's shell may export an account root besides,
which is the third layer of the chain that finds one (credentials.md §2). A
`pytest` run started the way above therefore carries live credentials whether
the suite wants them or not. That matters because **Pulumi prints a resource's inputs when an
assertion about it fails, and a provider's inputs include its credential**:
the first failing assertion in a suite that declares a provider renders
whatever the environment was holding into the report.

`tests/root_credentials.py` is the whole mechanism, and `tests/conftest.py`
applies it at import. Deliberately not through a fixture, not even one every
suite gets without asking: the earliest a fixture can run is the setup of the
first test, by which point every test module has been imported, so a suite
that read a variable while being collected would still have seen the
operator's value. It is anchored in `tests/conftest.py`, so it covers the
suites under `tests/` — which is all of them, and no `conftest.py` sits
above that one.

-   **What is masked** is every variable an account root can arrive in, read
    off `masters.ROOTS` rather than listed a second time, together with the
    other variables `mise.toml` lists under `redactions`. A root added to that
    register is masked by that addition alone.
-   **What is deliberately not masked** is `PGSSLROOTCERT`, `PGSSLCERT`,
    `PGSSLKEY` and `KLUSTER_KDBX`, named in `root_credentials.UNMASKED_PATHS`.
    Each carries a *path* to credential material rather than the material, so
    a failed assertion renders a path; a suite that needs the files behind one
    points the path elsewhere rather than blanking it. Every key `mise.toml`
    sets has to fall in one of the two sets, which is what makes a new one a
    decision somebody takes rather than an omission nobody sees.
-   **A suite that needs a value asks for a fake one by name**, with
    `root_credentials.fake_credentials('KLUSTER_B2_KEY')` as a context manager,
    or `root_credentials.fake(name)` for the value on its own. The value is
    derived from the variable, so a value that does reach a diff identifies
    what it stood in for and says that it opens nothing. A name that carries
    no masked credential is refused, because setting one would be relying on
    a protection that is not there. The exception is a case whose subject is
    the literal text of a credential — what a child process was handed, which
    layer answered — where the literal in view is the point and a derived
    value would hide it (`style/python.md`, "Not too DRY").
-   **A suite that needs one and does not ask** meets whatever the code under
    test raises for an unset variable, which names the variable it wanted.
    Nothing this masks reaches a stack program anymore: a provider credential
    is a secret in its own stack's configuration (credentials.md §1 rule 6),
    which a suite supplies with `pulumi.runtime.set_all_config` and which this
    mechanism has no part in.

**The environment is one of three channels, and the desktop secret store is
the second one closed.** An account root and the operator passphrase are
looked up in the desktop secret store, then in a file under the checkout's
own `.credentials/`, and only then in the variable (`kluster.lib.acquisition`,
credentials.md §2), and a `credentials` command writes the store as well as
reading it: `generate` and `recover` of the operator passphrase, `root
remember`, `kit password remember`.

-   **The store is closed for the whole session.** `tests/conftest.py`
    installs a `keyring` backend that refuses every call — what a machine
    with no store looks like, which every caller already handles — as a
    session-scoped fixture every case gets without asking. It is in place
    before any other fixture is set up and until the last is torn down, so a
    module's fixture that runs between two cases meets it as the cases do. A
    case run on a workstation can therefore neither read the operator's
    store nor replace a value in it with a placeholder. A case that needs a
    store installs the in-memory one from `tests/memory_keyring.py` over it,
    for its own length, and the end of that block puts back the store the
    enclosing block installed. No block asks `keyring` which store to put
    back, since asking resolves the machine's own wherever none has been set:
    `tests/test_harness_isolation.py` holds both, against a stand-in for that
    store which records being resolved.
-   **The file layer is still each suite's to redirect.** `workstation`
    resolves `.credentials/` from its own `__file__`, so on an operator
    workstation the file layer answers with live material without the
    environment being consulted at all. A suite that reaches `masters`,
    `workstation`, `kdbx` or the operator passphrase redirects it itself, by
    pointing `workstation.directory` at a `tmp_path` the way
    `tests/test_masters.py::local` does, or by handing the chain a checkout
    under `tmp_path`. Until that too is closed by construction, this remains a
    discipline rather than a property, and a suite that forgets it can print
    a credential that never went near a variable.

The asymmetry is the reason the three are not fixed the same way. A variable
can be read while a module is being *imported*, which is before any fixture
has run, so nothing short of stripping at import reaches it. The store and
the file layer are only ever reached from inside a test, so a fixture is early
enough — closing them is ordinary work rather than a place where the mechanism
has to be unusual.

The masking and the closed store cover the live tier too (§5). A drill reads
its credentials from the kit, never from the ambient environment.

### 1.2 Nothing a case starts outlives it

**A process a case starts goes through `tests/process_sessions.py`.** It
starts the command as the leader of a POSIX session of its own, and every
way out of the call -- a clean exit, a `TimeoutExpired`, an assertion, the
case bound, a `KeyboardInterrupt` -- kills every process in that POSIX
session and waits until each is gone. `run` is `subprocess.run` with the
output captured; `started` is a block that owns the command between its
start and its end, for a case that reads the command's output as it runs or
signals it. What the helper bounds is its own waits: `run`, and a
`Command`'s `wait` and `communicate`, take a `timeout=` with no default, and
the end waits at most `KILL_TIMEOUT` for each process to go. Setting each
`timeout=` below the case bound is the caller's part (§8). A read of the
command's output inside a `started` block is bounded only by the case bound,
whose failure unwinds through the end, unless the case bounds that read
itself.

The POSIX session is the unit because it is the one grouping every command
here keeps. A process group is not: the `pulumi` CLI starts each plugin in a
group of its own, and only a living `pulumi` closes them, so a `pulumi`
killed at its bound leaves its language host and its dynamic provider
running, the provider for good. Nor can the plugins be found as
descendants: a killed process's children pass at once to a parent the
case does not hold, so the tree the case started falls apart at the very
kill that needed it. Nothing leaves a POSIX session except by calling `setsid()`,
and Linux does not hand a session's id to another process while one member
lives, so the helper selects by membership alone: never by name, command
line, user or group. A process outside the POSIX sessions it started --
another worker's, another agent's gate, the operator's shell -- is never
signalled. `tests/test_process_sessions.py` holds each way out, the helper's
precision, and the premise that the CLI's plugins stay in its session, so a
release that moves them out fails naming the premise.

What it does not reach is, by definition, anything outside the POSIX session
of a command it started, and anything at all once the test process can no
longer run a `finally`. Today's instances:

-   **A descendant that calls `setsid()`.** None of the adopting suites'
    commands has one, and the CLI's plugins do not.
-   **A process code under test starts through a runner of its own** --
    `pulumi_cli.run_pulumi`, `github_secrets.run_gh`, the client tools
    `state` runs. Those keep `subprocess.run`'s kill of the child alone.
-   **The test process killed outright.** SIGKILL, or the outer `timeout`'s
    SIGTERM (§1), which Python turns into no exception, runs no clean-up.
    And because each command leads a group of its own, the outer `timeout`'s
    signal to the run's process group no longer reaches it, as it reaches a
    command that shares the run's group: a shell blocked on a FIFO outlives
    such a kill. That is the price of the per-case guarantee, and the outer
    `timeout` stays the hang guard for what the per-case bound cannot reach.

**The case bound is one shot.** `pytest-timeout` arms its alarm once per case,
over set-up, call and teardown, and its handler raises a failure inside
whatever the case was doing. Landing in a block, that failure unwinds through
the end, whose waits are bounded, so the case fails by name and the run goes
on to its summary. The end runs a second time in a `finally` of its own, for
an alarm that lands inside the first. After the alarm, the rest of the case's
teardown runs with no bound at all, so a fixture that keeps a process keeps
it inside `started`, and every other teardown wait carries a `timeout=` of
its own.

**A command a case starts reads only the stdin the case hands it**, and
`/dev/null` otherwise: never the test process's own, which under `-s` is a
terminal. A fake reads its stdin only where the tool it stands in for does.

**A container a case starts carries `--rm`, a `--timeout` at the run's outer
bound, and the test label**, `--label kluster-test=<module>`. Its processes
are the container service's rather than the case's, so it is the one kind of
process that ends by itself however the test process ends, and the label is
the census of what is left.

A suite that still starts a process through `subprocess` is a finding when
that process can outlive its own kill: one that starts processes of its own,
or blocks on a pipe, a FIFO or a service. Each of today's is a slice of
Aetf/kluster-ops#530's plan.

## 2. Writing Tests

A suite that declares resources starts from `tests/mock_monitor.py`, which
carries the three pieces every such suite needs. It is a named module rather
than a `conftest`, because test modules import it and `conftest` is not a name
an import can aim at.

-   `Recorder` is a `pulumi.runtime.Mocks` that invents nothing: it answers
    each registration with the resource's own inputs and an id built from its
    logical name, and remembers every declaration -- its type, its inputs and
    the provider instance it was registered against.
-   `run_with` points the runtime at a monitor and hands it back, through
    `set_mocks`, which builds each run a fresh registration queue.
-   `declaring` is the barrier. Declaring a resource only schedules its
    registration, so without it the monitor has seen nothing and every
    assertion about it passes vacuously.

What a suite writes for itself is only the part that is its subject: an output
the provider computes that the inputs do not carry, and the answer to an
invoke.

**What two test modules share lives in a named module, and no test module
imports another.** A test module is a file `pytest` collects, a `test_*.py` or
a `*_test.py` (its default `python_files`). A helper, a fixture's body, a fake
or a table that two of them share by import goes into a module under `tests/`
named for what it holds, which `pytest` does not collect, and each of them
imports that: `mock_monitor`, `workflow_files` and `section_numbers` are the
form. A fixture `pytest` hands a case without any import may live in a
`conftest.py` instead. The rule is about the import, not what it fetches, so
any module that imports a test module breaks it, a helper module included.
Two things make such an import wrong even where it works:

-   **It reaches the module `pytest` collected by an accident of layout.** A
    test module is `pytest`'s to import, under the name its import mode gives
    it. In the default `prepend` mode, which `pyproject.toml` leaves in place,
    that is the file's bare name: neither `tests/` nor `tests/live/` is a
    package, and `pytest` puts each directory it collects from on
    `sys.path`. So `from test_x import …` binds the module `pytest`
    collected only because the two names coincide. Under
    `--import-mode=importlib` the name is `tests.test_x`, and the same
    statement loads a second copy, its module-level code and state
    included. A helper module is imported by the suites alone, under the one
    name they spell, so there is one copy of it in any mode. Both
    directories put their modules at the top level, which is also why no
    two helper modules across them share a name.
-   **It brings the whole module for one name.** Importing a test module
    runs all of its module-level code, its imports and its tables, for the
    one name the importer wanted, and a `test_` function the import names
    is collected in the importer and runs a second time there.

`tests/test_suite_imports.py` holds the rule, and the unique names, over every
module under `tests/`.

**An async fixture scoped wider than a case states its `loop_scope`.** In the
strict mode the suite runs in, an async fixture is one decorated with
`pytest_asyncio.fixture`, and `pyproject.toml` sets
`asyncio_default_fixture_loop_scope` to `function`, so such a fixture runs on
its case's event loop unless it names another. One whose `scope` is wider,
such as a module's `applied` run, outlives that loop, so it names its own,
the same as its scope: `@pytest_asyncio.fixture(scope='module',
loop_scope='module')`. Without it, `pytest-asyncio` refuses the fixture only
when a case first asks for it, with a `ScopeMismatch` on
`_function_scoped_runner` that names neither the fixture's module nor the
missing argument. `tests/test_suite_fixtures.py` holds the rule over every
module under `tests/`, a fixture no case uses included.

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
    """What the account decides: the prefix it assigns, and its service catalog."""

    def computed(self, args: pulumi.runtime.MockResourceArgs) -> dict[str, Any]:
        if args.typ == 'oci:Core/vcn:Vcn':
            return {'ipv6cidrBlocks': ['2001:db8::/56']}
        return {}

    def answer(self, args: pulumi.runtime.MockCallArgs) -> dict[str, Any]:
        if args.token == 'oci:Core/getServices:getServices':
            return {'services': [{'id': 'ocid1.service.test', 'name': 'OCI Object Storage', 'cidrBlock': 'oci-os'}]}
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

### 3.1 What the mock monitor drops, and what it lets through

Three ways Pulumi's own mock monitor answers a registration differently from
the engine matter to a case, and `tests/mock_monitor.py` closes all three by
patching `MockMonitor.RegisterResource` once, at import, before any Pulumi
code runs. Two are things the mock drops that a case may want:

-   **`propertyDependencies`.** During registration the engine is told which
    outputs a resource property depends on; the mock monitor's response drops
    them, so a dependency assertion would always come back empty. The patch
    copies them from the request onto the response.
-   **The resource options.** `import`, `ignoreChanges`, `deleteBeforeReplace`
    and `dependsOn` reach no output at all. The patch keeps each registration
    request on the `Recorder` the run was built around, where
    `Recorder.options_of(name)` and `Recorder.depends_on(name)` read them, and
    every request in registration order in `Recorder.requested`.

The third is something the mock lets through that the engine refuses:

-   **A repeated resource identity.** A URN is a resource's identity, and a
    program that registers one twice is a program the engine stops
    (`Duplicate resource URN <urn>; try giving it a unique name`). The mock
    monitor lets the second registration silently replace the first, so a run
    no engine would accept reads back as a run with one resource in it. The
    patch refuses the second registration as the engine would, keyed by the
    URN the mock computes — stack, project, the parent's type, the type,
    the logical name. That key qualifies by the *immediate* parent's type
    where the engine carries the whole chain, so the refusal is at worst
    stricter than the engine's and never laxer; and, like the engine's, it
    does not carry the parent's *name*, which is why a child's logical name
    carries the `name` of the component that holds it — the rule under
    "Resources and their contents" in
    [style/pulumi.md](../style/pulumi.md).

A suite meets the refusal when one run legitimately wants two of something:
a variant of a component built beside its baseline, to compare the two. The
move is to give the variant a distinct logical name — a builder that takes
the name as a parameter, the way `build_cluster` in
`tests/test_talos_day1.py` does — not to loosen the refusal, because the
engine would stop the same run.

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

### 3.3 Unknown values

An unknown awaited by an `async_output` coroutine aborts it and leaves that
one output unknown. **The abort does not ask which kind of run it is in**, so
a mock run built with `preview=False` reaches it exactly as a `preview=True`
one does; how a run comes to hold an unknown and what the engine does with it
is [pulumi.md](pulumi.md) §1.2. A case about this path therefore names the
value that is unknown rather than the flag the run was built with, and the
`unbuilt` fixture in `tests/test_async_properties.py` is parameterized over
both kinds of run for that reason.

Assert on the output, not on the exception, which `async_output` catches by
design:

```python
from pulumi.output import Unknown

network_id = my_component.subnet.network_id
assert isinstance(await network_id.future(with_unknowns=True), Unknown)
assert await network_id.is_known() is False
```

**How a mock run produces an unknown is not how a real one does.** The engine
reports a create it skipped by setting `unknown` on the
`RegisterResourceResponse`, which the SDK turns into
`resolve_missing_as_unknown` for that resource's outputs;
`MockMonitor.RegisterResource` never sets that field, so the real mechanism is
unreachable from a test. A case models it in one of two ways:

-   **A dependency's `id`.** The mock returns `pulumi.UNKNOWN` as the `id` of
    a resource the subject reads, which reaches the same `Output` state and is
    a fair proxy for one output of one resource.
-   **A declined invoke.** The engine gates an invoke on its dependencies
    having been created and, while one is pending, answers with
    `ResourceInvokeResponse.unknown` set rather than calling the provider. The
    mock monitor never sets that field either; `decline_every_invoke` in
    `tests/mock_monitor.py` makes the run's monitor answer every invoke that
    way, which is the proxy for a lookup the subject awaits through `resolve`.

Neither proxy carries:

-   **Transitivity.** The engine leaves everything downstream of a skipped
    create unknown; the double leaves unknown exactly the property the mock
    wrote, so a case that reads the abort as propagating is asserting
    something the double never modeled.
-   **The meaning of a property the mock omits.** Outside a preview such a
    property resolves as a **known `None`** rather than as unknown:
    `pulumi/runtime/rpc.py` computes
    `known = not settings.is_dry_run() and not resolve_missing_as_unknown`
    for every resolver whose key the response is missing, and under the mocks
    the second half is always true. A case that withholds a property
    expecting an unknown therefore passes in a `preview=False` run without
    ever reaching the abort it is named for.

**A nested unknown reads back as the run's own kind, and it is the double
that makes it so.** An unknown nested in a property the program handed over —
`imageSourceDetails.sourceUri`, say — is read back off the recorder rather
than off an `Output`: the mock deserializes the registration's inputs, and
`rpc.deserialize_property` turns an unknown into an `Unknown` under a preview
and drops the key otherwise. It does that on an executor thread, which the
pinned SDK runs under a copy of the registering context
(`pulumi.runtime._context.wrap_with_context`), so `is_dry_run()` there answers
for the run that `run_with` set up: a preview reads back an `Unknown` and an
update a dropped key, whatever ran before in that context. The pin,
`tests/test_mock_monitor_unknowns.py`, holds two runs of different kinds in
one case, in each order, because one context is where an answer left by an
earlier run would show: the test runner hands each case a fresh one. The
assertion that holds under either kind of run says what the case means
rather than which shape the SDK chose:

```python
assert not isinstance(details.get('sourceUri'), str)
```

That is, no value reached the provider under that key.

## 4. Fakes and the Ratchet

Code that drives an external service is tested against a fake of that service
(the tenancy in `tests/oci_tenancy.py` is the worked example). A fake is
not a stub that returns success; it is the smallest model of the service that
can still tell a correct caller from an incorrect one.

**A fake carries authorization semantics.** It records which principal made
each call, so a test can assert *who* did something and not merely that it
happened. Whether a rotation's sweep runs as the successor or as the
predecessor it is retiring is the whole of one defect — a session that
deletes the key it signs with cannot finish the sweep — and it is only
visible because the fake tenancy records every call under the user and the
key that signed it: either principal leaves the same keys behind.

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
RUN_LIVE_DRILLS=1 timeout 2400 mise x uv -- uv run pytest tests/live -n 0 -s --log-cli-level=INFO
```

A drill runs under no per-case bound: its duration is the provider's — a
rotation waits for the tenancy to authenticate the key — so
`tests/live/conftest.py` marks every item under the directory
`timeout(0)`, which outranks the bound §1 configures, and the outer
`timeout` on the command above is the only guard a drill runs under. So it
sits above the longest the drills' waits can take while each is still
inside its deadline: the OCI seed drill's `WORST_CASE`, derived beside
its constants, with room above it for the password prompt and the calls
between the waits. `tests/test_gate_command.py` holds the
command's bound above that sum, so a deadline that grows fails there
first.

`tests/live/conftest.py` is the entire mechanism: without `RUN_LIVE_DRILLS=1`
it declines to collect the directory, so an ordinary `pytest` run neither
executes a drill nor reports one as skipped. There is no marker to keep
in sync. The command's `-n 0` overrides the workers `addopts` starts
(§1): a drill runs in the `pytest` process itself, because a worker's
output and its live log reach no terminal.

A drill reads its credentials from the same store the command-line entry point
uses — for the credential drills, `KdbxStore.from_env` on `$KLUSTER_KDBX`. It
cannot read them from the ambient environment, nor take the kit's master
password from the desktop secret store: §1.1 masks the one and closes the
other for the whole process, the live tier included. Pass `-s`, so the
password prompt reaches a terminal.
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

### 5.1 A scratch probe names its own backend

A drill is meant to reach the live installation; a **scratch probe** is not — a
throwaway project or `stack init` run to see what the CLI does, against a
`file://` backend of its own. The one way a probe goes wrong is by landing
in the live backend, and the route there is `mise x`: `mise.toml` resolves
`PULUMI_BACKEND_URL` and the passphrase from the checkout's slots **ahead of
the shell** (the slot is the source), so an exported `file://` URL is
overridden wherever a slot answers. The slots are the ones in the checkout
that holds `.credentials/` — on a workstation, the primary checkout — and
they answer anywhere in it except under its `.claude/`. Every `jj`
workspace sits there: mise renders the enclosing checkout's `mise.toml` as
well when it runs in a workspace, and under `.claude/` those templates
yield nothing, so a workspace's `mise x` hands on what the shell exported
(`kluster-ops#387`). A `pulumi` run that needs the slots therefore runs
from the primary checkout, never from a workspace.

A probe still never runs `pulumi` through a checkout's `mise x`, `-C`
included: in the primary checkout the slot wins, and `-C` reaches that
checkout from anywhere. It calls the binary mise resolves, and sets its
backend in that same command, with every scratch path under the
workspace's own `.claude/`:

    PULUMI_BACKEND_URL=file://<scratch>/state PULUMI_HOME=<scratch>/home \
    PULUMI_CONFIG_PASSPHRASE=<anything> "$(mise which pulumi)" stack ls --all

`PULUMI_HOME` is part of the form because `pulumi` otherwise reads and
writes the operator's `~/.pulumi`, which records a logged-in backend of
its own. **Read `pulumi stack ls --all` back before any `stack init`**, in
the same command form: it names what the backend in hand holds, and a list
naming this repository's stacks means the probe is pointed at the live
backend. `--all` is what makes that read mean anything — a bare `stack ls`
lists the current project's stacks only, so a scratch project reads back empty even
against the live backend.

## 6. Proving a Test Fails Without the Change

AGENTS.md requires that new behavior ship with a test that fails without
it. That is a claim about the test, and the only thing that establishes
it is breaking the behavior and watching the test go red. The broken
version — the mutation — is throwaway code, and the whole of the
discipline below is about getting rid of it again without taking the
real change with it.

**Keep a pristine copy and mutate in place.** The throwaway artifact is
the copy, not the mutation, and it lives in the workspace's own
`.claude/mutation/`, which `.gitignore` ignores — on the same filesystem
as the file, which the last command needs:

```bash
mkdir -p .claude/mutation
cp <file> .claude/mutation/orig
# every run in the round, the baseline, each mutation, and the run after the restore:
rm -rf "$(dirname <file>)/__pycache__" && PYTHONDONTWRITEBYTECODE=1 \
    timeout 1200 mise x uv -- uv run pytest -p no:cacheprovider <cases>
# mutate <file> in place and run as above; restore, then run as above once more:
cp .claude/mutation/orig .claude/mutation/back
mv .claude/mutation/back <file>      # restore: a rename, not a write through <file>
```

Mutating a copy beside the original and running the suite against *that*
does not work: the suite imports the package from the import path, so
the run is against unmutated code and passes — which in a mutation round
is the alarming answer, and reads as "the new test does not bite".

**Every run in a round compiles the mutated file from its source,
because the interpreter's cache of compiled modules cannot see an edit
made within the second it records.** Python reuses a module's cached
`.pyc` whenever the source's modification time, truncated to whole
seconds, and its size both match the values in the cache's header — the
default timestamp invalidation of
[PEP 552](https://peps.python.org/pep-0552/) and the
[`py_compile` documentation](https://docs.python.org/3/library/py_compile.html#py_compile.PycInvalidationMode),
checked on import by `_validate_timestamp_pyc` in
`importlib._bootstrap_external`. A one-character mutation keeps the
size, so any write that lands in the second the cache was compiled from
is invisible to the next run, and a restore is such a write as much as a
mutation is. The cache `pytest` keeps of rewritten test modules follows
the same rule, so mutating a test is exposed the same way. Both ends of
the round are open:

- **A false green.** A mutation that lands in the second recorded in the
  cached `.pyc` runs the pristine code, and the test that should go red
  passes — the round reports that the test does not bite.
- **A false restore.** A restore written in the same second as the
  mutation it undoes leaves the mutated code cached, and every later
  run, the gate included, executes the mutation while the pristine
  source sits on disk: a red suite over a tree that `jj diff --stat`
  and a read-back both call clean.

Neither is rare in a scripted round. A narrow selection runs in a second
or two, so a driver that mutates, runs and restores back to back puts
consecutive writes into one second routinely.

Each part of the run line closes one path. The purge removes whatever
`.pyc` an earlier run left beside the file, because
`PYTHONDONTWRITEBYTECODE` stops writes and not reads, and a `.pyc` a
plain run wrote is still served. The variable keeps the round's own runs
from writing any, so no compiled mutation outlives the round, and a
plain gate run after it compiles the restored source whatever second it
lands in. `-p no:cacheprovider` keeps out of the round the state
`pytest` itself carries from run to run, the last-failed set that `--lf`
and `--ff` read and the position `--sw` resumes from: on the suite's
default command line it changes nothing, and it is there so that a run
given one of those options does not choose its cases from another run's
result. `mise x` and `uv run` both hand the environment to the
interpreter unchanged, so the variable in front of the command reaches
it. The purge names every directory holding a file the round mutates, a
test file included.

A fresh `PYTHONPYCACHEPREFIX` per run is equally immune — the
interpreter then reads and writes compiled modules only under that
directory — but it moves every module's cache, the standard library's
and every dependency's included, so each run recompiles all of them and
leaves a tree of `.pyc` files behind that is one more thing to clean up.
The purge touches the one directory the round changed and writes
nothing, which is why it is the form.

**A stale answer is keyed to the file's modification time, not to the
clock**, so it does not go away on a re-run: a stale green stays green
and a stale red stays red however often the suite runs. What flips it
leaves the code as it was — the file touched or rewritten a second
later, or the cache purged. A surprise in a round — a mutation that does
not bite, a restored tree that will not go green — is re-run once in the
form above before it is believed, and an answer that changes when only
the cache changed was never about the code.

**A second tree measures only what it imports, so its environment is
built there and never linked in.** The same proof run against the old
code — a scratch checkout of `main` beside the workspace, to show a case
reddened before the change and not after — is a second tree, and a
second tree given a copy or a hardlink of another checkout's `.venv`
runs that checkout's package while collecting this tree's tests. Two
absolute paths do it: the console script's shebang — the first line of
`.venv/bin/pytest` is `#!` and the absolute path of that checkout's
`.venv/bin/python` — and the `.pth` file the editable install of
`kluster` leaves in `site-packages`, naming that checkout's `src`. `uv
run` execs the `pytest` script, which execs the other interpreter,
and the other interpreter imports the other `src`. The obvious spot
check passes on the same tree: `uv run`'s own sync reinstalls the
editable package, so `uv run python -c 'import kluster;
print(kluster.__file__)'` names this tree, while the script it does not
reinstall keeps the shebang. Nothing errors, and the run reports the
answer the round was hoping not to see — green on the old code — which
is the direction that ends an investigation. So the environment is built
in the tree, `mise x uv -- uv sync` from its root, which is not the
expensive step it looks like because `uv` links packages out of its
cache rather than fetching them; and the run asserts what it imported
rather than trusting the recipe. A `pytest` plugin that raises from
`pytest_sessionstart` unless `kluster.__file__` resolves inside the tree
under test is the form that has worked — loaded with `-p <module>`,
which imports by name off `sys.path`, so a plugin kept under the tree's
`.claude/` needs `PYTHONPATH=.claude` on the command, or it fails with
`No module named` before any test runs: the run either measures the tree
it claims to or refuses, and a refusal is the one answer that cannot be
misread.

**The restore above is a rename because a write into a tracked path is
a window.** `>` truncates its target before the writer produces a byte —
`{ stat -c %s f; } > f` reports `0` on a file that was not empty an
instant earlier — and the file stays short until the writer finishes.
`jj` snapshots the working copy on every command, several workspaces
share one store, and a dispatcher's `jj git fetch` while a builder is
live is routine ([dispatch.md](dispatch.md) §1.2), so concurrent
snapshots are the normal case here rather than the exception and one
landing inside the window is a matter of timing alone. It records the
empty file as the working copy's honest content, and
`jj workspace update-stale` then restores exactly that: a 0-byte source
file, which no edit produces and which the gate reports as a cascade of
import errors rather than as corruption. A rename within one filesystem
replaces the name in a single step and has no such window; across
filesystems `mv` is not a rename, which is why the scratch path is
inside the workspace rather than under `/tmp`. Where `sponge` (from
`moreutils`) is installed it buffers the same way in one command.

The rule generalizes past this recipe: **any `>` into a tracked path is
a window a concurrent snapshot can see.** The natural way to revert a
file to `main`'s copy is one of them —

```bash
jj file show -r main <path> > <path>          # not this: truncates <path> first
jj file show -r main <path> > .claude/mutation/back && \
    mv .claude/mutation/back <path>           # this: the same revert, no window
```

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

**What makes that bare form a revert is the source it defaults to** —
the working copy's *parent* — and naming a source can negate it.
`jj restore --from @ <file>`, run inside the workspace whose working
copy *is* `@`, restores the file from itself: it prints
`Nothing changed.`, exits 0, and leaves the mutation exactly where it
was. That output reads as "the file already matched" rather than as
"the revert did nothing", which is the whole difficulty. It is the trap
below with the sign reversed — `git checkout HEAD -- <file>` destroys
silently, this changes nothing silently, and either way the operator
believes a revert happened and reads the next run as evidence about the
real change. So the source has to be a commit other than the one holding
the mutation, and **after any revert, verify the file rather than the
exit status**: the next variant will be spelled differently, and an
exit status is the one thing each of these failures shares with
success. In a mutation round the run itself carries the tell — a failure list that still names
the test which should have gone green is the no-op showing through — and
a round read as pass/fail counts alone has nothing that disagrees.

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

**The bare `jj restore <path>` has a third failure of its own, and it
belongs to the fix cycle.** The source it defaults to is `@-`, so what
that revert means depends on which round is running. On a first round
`@-` is the branch's last commit and the restore returns the file to
the state the branch already had. On a **fix round with uncommitted
work** `@-` is the *previous* round's commit, so the same command rolls
that file back past the mutation to before this round's edits — exit 0,
and the ordinary `Added 0 files, modified 1 files` for output. **The
gate does not disagree either**, because a round's source edits and its
test edits sit in the same change: a restore that takes both leaves a
consistent tree and a passing suite, and only the files nobody restored
still carry the round.

The rule that makes it safe is the `jj new` above applied to the round
rather than to the mutation. Describe and commit the round's work
first, so `@-` is the base the restore should return to and the
mutation is alone in `@` — or restore from the pristine copy, which
answers about no revision at all.

Those three — `git checkout HEAD -- <file>`, `jj restore --from @
<file>`, and the bare `jj restore <path>` mid-fix-round — share one
shape: a restore that silently does something other than what was
asked, in a tree whose gate can then pass. A green suite is evidence
about whatever the tree now holds, not about the change under test.

**What a bad restore took is still readable**, and the first move on
noticing one is to read it rather than to reconstruct it from memory.
`jj` snapshots the working copy on every command and a working copy is
a commit, so the content is in the operation log:

```bash
jj op log                                   # the snapshot before the loss
jj --at-operation <op> file show <path>     # print that content back
```

The read prints to stdout and changes neither the working copy nor the
repository, so it costs nothing to run before deciding anything;
`jj undo` reverts the restore itself where it is still the last
operation. Which snapshot to name depends on what ran between the
mutation and the restore: the operation immediately before it holds the
mutation along with the round, and an earlier one holds the round's work
clean if any command ran before the mutation was written.

**With the round committed, an empty `@` is what says the mutation is
gone:**

```bash
jj diff --stat
```

`0 files changed` is the whole result, and it is decisive only because
the round's work is at `@-`: `@` holds the mutation and nothing else,
so the question is zero or non-zero rather than a summary to interpret.
**`jj st` is not that check** — piped through `head` it truncates
exactly the list the discipline exists to surface, so a restore that
took several files can read as one.

That check is also what an interrupted round owes. **A mutation left in
the tree is handed back by every recovery**, because a workspace picked
up again carries exactly what was last recorded — an interruption
mid-round, `jj workspace update-stale`, a rewrite from another
workspace, all alike. The deliberate defect comes back indistinguishable
from the author's own code: no warning, no marker, and a failing suite
that reads as the change under test being wrong. So work resumed after
an interruption begins by establishing what is in the tree — `jj log`
and `jj diff` before anything is edited — rather than by continuing from
memory, and a mutation round is over when `jj diff --stat` says zero,
not when the author believes the last restore ran.

**A restore is confirmed, never assumed, and the mechanism that was to
produce it is not what confirms it.** A loop that mutates, runs, and
restores in a `finally` leaves the file mutated when a tool timeout
kills the process mid-case, because nothing lives to run the `finally`;
a driver whose last stanza restores a copy taken before the fix puts the
pre-fix file back over the fix; an edit that never applied leaves the
tree unmutated and the round measuring nothing. Each of them leaves a
tree that looks like one nobody touched, and the code that was supposed
to restore reports nothing either way. What confirms the restore is the
tree: `jj diff --stat` at zero with the round's work committed below it,
or, for a file the round holds uncommitted, the source read back and
compared against the pristine copy. Reading it back is part of the
round, not a recovery step for when something looks wrong.

**After any mutation round, verify the diff against the branch's own
base:**

```bash
jj diff --from main
```

Without `--stat` here: zero against `@` above is a yes-or-no answer,
while this comparison is not. The summary is decisive only for a
whole-change loss (`0 files changed`); after a partial one it reads
`1 file changed, 1 insertion(+)`, which helps only a reader who
remembers it should have been two. The full diff shows both halves of the question — that the
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
3.  **Always Use Timers**: Run the suite under the outer `timeout` — the form
    in §1 — for what the per-case bound cannot reach, and leave that bound in
    place: it is what ends a coroutine that never resolves its futures, by
    name.
4.  **Keep it Fast**: Unit tests should not make network calls or create real
    resources.
5.  **No Wall-Clock Budgets**: A unit case's outcome never depends on how
    much real time passes. A budget measured against the real clock is also
    a budget on the *process*: a machine stalled past it -- swap, a
    contended runner -- fails the case with a failure that names nothing
    distinguishing the stall from the defect, and a failure that names
    nothing is a flake nobody can aim a fix at (Aetf/kluster-ops#243 is the
    record). Two forms replace it:
    -   A **bounded wait** in the code under test gets a clock the wait
        itself advances: the module under test's own `time` name is replaced
        by a clock whose `sleep` moves its `monotonic`, so the wait's own
        sleeps move the clock, and a deadline is still reachable without a
        second of wall time. The name in that module rather than the
        process-wide `time` module, so that everything else a case runs —
        a child process's poll loop, the per-case bound — keeps the real clock.
        `oci_clock`, installed as `unhurried` by every suite that reaches
        `oci_iam`'s waits, is the form.
    -   A **hang guard** is bounded in turns of the event loop --
        `asyncio.sleep(0)` yields exactly once, whatever the machine is doing
        -- or left to the bounds the gate already runs under (item 3).
        Never `asyncio.wait_for(…, seconds)`: a guard in turns fails as "not
        settled after N idle iterations", which no stall of any length can
        produce, and one in seconds fails as whatever the deadline cut off.
        A turn bound is sound only for a path the loop alone advances. Under
        the mocks a resource registration crosses the SDK's executor thread
        (`tests/mock_monitor.py`, `_capture_request`), so around anything that
        awaits a registered resource's output the turns run out in
        microseconds while the thread is still working, and the guard fails
        by how loaded the machine is -- the flake in a new shape. That path
        is left to the gate's bounds. The per-case bound among them is the
        one real-clock bound a case runs under, and it is admitted because
        it fails differently: an order of magnitude above every case it
        bounds, and with the stack the case hung in, which is what tells a
        stall from a hang (§1).
        A guard that stays in seconds is a wait the loop does not run,
        where nothing yields to count -- the `timeout=` handed to
        the helper's `run` and `wait` (§1.2), or to `subprocess.run` or
        `Popen.wait`, a thread's wait on an event --
        and it fails naming what it waited for, which is a failure with a
        name.

## 8. Engine Tests

**An engine semantic a design depends on and no mock can show is pinned by an
engine test.** The mock monitor answers a registration and records its
options; it never plans a step, runs a hook or orders a replacement, so a
design resting on what the engine does with those options is resting on
something only the engine can answer. `tests/test_state_backend_engine.py`
holds what the `state-backend` stack's hooks rest on (rfc-006 §4.3): a raising
`before_delete` hook leaves its resource and fails the run, and a raising
`before_create` hook leaves it uncreated; a passing `before_delete` hook runs
before the delete; a preview runs no hook; a raising `after_create` hook
leaves its resource recorded and fails the run; a replacement, targeted or
not, is created from the program's value of an `ignore_changes` input; and a
dependent declaring `replace_on_changes` on the replaced resource's id is
replaced with it and runs its `after_create`, where one that does not is
updated and runs nothing.

It runs in the suite, under the bounds every real-engine case carries
(below):

-   **The CLI is the one `mise.toml` pins**, found on `PATH`, so a pin bump
    reruns the test against the release it moves to. A run with no `pulumi`
    on `PATH` skips it, saying so.
-   **Its backend and its home are its own, named on every process it
    starts** — a `file://` directory and a `PULUMI_HOME` under the case's
    temporary directory, with every `PULUMI_` and `PG` variable of the test
    run's own removed — as they are for a scratch probe (§5.1), and it reads
    `pulumi stack ls --all` back as empty before its first `stack init`.
-   **The program runs on the locked SDK and fetches nothing.** Its virtual
    environment's `.pth` file reaches the test run's own packages. A `uv`
    environment carries no `pip`, which the language host asks for unless
    the project's `toolchain` option is `uv`, and under `uv` it asks for a
    lock beside the project, which `uv lock --offline` writes.
-   **Each case is a few commands against a fresh stack**, under the
    real-engine bounds below, and the order the engine ran things in is
    read from a log every provider method and hook appends to, rather than
    from the CLI's output.
-   **What one release does and the next stops doing is not held.** A test
    that pinned it would fail on the bump for a change that removes nothing
    the design relies on.

`tests/test_extension_provider.py` holds what the CRD extension rests on
(kluster-ops#160): a resource of an extension package -- a `crds:` resource
of the Kubernetes provider's -- lands on the explicit `kubernetes` provider
it is handed, whether through the option, the `providers` map or list, or a
parent that carries it, with the default provider turned off as every stack
here turns it off. The SDK settles that only once the extension's package
registration resolves, and the mock monitor answers that registration
without any provider seeing the extension, so only a `preview` against the
engine shows it. A release before 3.266.0 sends the resource to the default
provider instead, which is what the `pulumi` floor in `pyproject.toml`
says; what the case holds is something the design relies on, which the
rule above leaves in.

It runs as the case above does, except where these say otherwise:

-   **It fetches the Kubernetes provider plugin**, the one exception to
    fetching nothing. The extension is that provider's, so generating it
    and previewing it both need the plugin, at the version the locked
    `pulumi-kubernetes` registers, and no stand-in would exercise the
    provider's own handling of its extension.
-   **The plugin is downloaded once per worker that runs the module's
    cases**, into a `PULUMI_HOME` of the module's own that the module
    removes when it finishes, rather than into one per case; each case
    still has a backend of its own.
-   **The provider the engine planned is read from `pulumi preview --json`**,
    the step for the resource naming it, rather than from a log.

**A case that starts the pinned `pulumi` CLI carries bounds set from its
measured duration, not the suite's per-case bound.** That is the
definition, so a module that comes to start the CLI is under it without an
edit here. The census is a run with a `pulumi` first on `PATH` that logs
the case it was started from and hands over to the pinned one. Today's
are:

-   the two modules above;
-   the `real` cases of `tests/test_operator_stack.py`, which run the
    operator driver against a scratch stack;
-   the cases of `tests/test_pulumi_config.py` that take a real-CLI
    project, and its failing-invocation case;
-   `tests/test_derived.py`'s `test_the_token_lands_where_the_program_reads_it`;
-   `tests/test_process_sessions.py`'s
    `test_the_pulumi_clis_plugins_stay_in_its_session_and_go_with_it`, the
    premise `tests/process_sessions.py` rests on (§1.2);
-   `tests/test_state_roles.py`, whose stacks are made and listed with the
    CLI: each command it starts, and each `pulumi` and client-tool run of
    the code under test, at its `COMMAND_TIMEOUT`, and each case at its
    `CASE_TIMEOUT`, above every command bound, including the one `render`
    gives `butane` in the module's set-up.

Others start the CLI and run under the suite's bound, short of the rule
until the issue named beside each gives them bounds of their own:

-   `tests/test_sealing.py`'s `test_the_write_is_held_to_what_the_real_cli_accepts`
    (Aetf/kluster-ops#535);
-   `tests/test_pulumi_package_checksums.py`'s `test_the_entry_without_a_map_loads`
    and `test_an_entry_carrying_a_sum_is_refused` (Aetf/kluster-ops#535).

Their durations grow with the machine's load far more than a mocked case's
duration does, because each runs the CLI, and most a language host and a
provider, as processes of their own. Measured on four cores under the
suite's own four workers (§1), the slowest real-engine case took 10 s, and
28 s with twelve busy processes beside the workers, four per core; the
slowest real-CLI case took 11 s and 39 s. The suite's 60 s is not an order
of magnitude above that, and a shared CI runner is a loaded machine. A
module that meets the rule states its bounds as named constants, with the
measurement in their comment:

-   **The case bound** covers the case's set-up and every command it
    runs: `pytest.mark.timeout` with the module's constant.
-   **The command bound** is the `timeout=` of each `pulumi` process, and
    of any wait on one, and sits below the case bound, so a stalled
    command fails as a `TimeoutExpired` naming it before the case's bound
    fires. A process a case started and stops waiting on is killed with
    its POSIX session (§1.2), never waited on without a bound.
-   **Both are stop-losses.** Each sits several times above the worst
    measured under load, and nothing asserts on elapsed time (§7 item 5).
    A bump that measures a case slower moves the constant and its
    comment together.

The load such a case meets is the machine's and the suite's own: three
other workers (§1), any of which may be running a case of this kind at the
same moment. That is the load the bounds above are measured under, and
what keeps the cases apart is that each one's backend, home and project are
its own.
