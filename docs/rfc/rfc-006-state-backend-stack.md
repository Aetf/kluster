# RFC 006: The State-Backend Appliance as a Pulumi Stack

*   **Status:** Accepted, 2026-09-28. The operator approved the design
    with every question of §15.1 answered at its recommended option; the
    answers are written into §15.1. The approach — the whole appliance is
    one Pulumi stack — was ruled on the design this document was promoted
    from, together with three things that design lacked, and this one
    carries: the state's home argued with a `file://` backend committed to
    this repository as a first-class option (§3), `deploy/state-backend/`
    absorbed into `src/` with its driver a console script like the others
    (§8), and one way of driving the two stacks an operator runs by hand
    (§9).
*   **Created:** 2026-09-27
*   **Authority:** AGENTS.md,
    [framework/dispatch.md](../framework/dispatch.md),
    [framework/rfc.md](../framework/rfc.md) and the style rules
    (`docs/style/`) are what this document obeys. A rule proposed here
    that they do not state is marked **new rule** where it is stated.
*   **Companion:** [rfc-002](rfc-002-src-layout-and-the-gateway.md), for
    the layering, the explicit provider (rfc-002 §8.1), the dynamic
    provider that carries no connection state (rfc-002 §7.4) and the
    configuration file rendered from beside its module (rfc-002 §9.1).
    Its precedents are cited at each use rather than re-argued.
*   **In scope:**
    1.  where the appliance stack's state lives, and what a run owes when
        that home is this repository;
    2.  the appliance declared as one stack: every entity and what
        declares it, the options its instance needs, the dump and restore
        around a replacement, and what stays outside Pulumi;
    3.  the keys the box carries, made stable, and where each is held;
    4.  the operator passphrase: what it covers, and how a run finds it;
    5.  `deploy/state-backend/` absorbed into `src/`, file by file;
    6.  one console script that drives every stack an operator runs by
        hand, `github` and `state-backend`;
    7.  the rules those need, and the slices.
*   **Out of scope:**
    *   **The appliance's own design where nothing here moves it**: Fedora
        CoreOS, Postgres and its roles, the offline CA, the nightly dump
        and its encryption, and the probe. Those are
        [physical/state-backend.md](../physical/state-backend.md) §1–§6,
        which change only where §13 says.
    *   **The `github` stack's declarations**, which are
        [framework/github.md](../framework/github.md) §3. This document
        changes how that stack is run, and where its passphrase is found.
    *   **Where the other stacks' state lives**: the appliance's Postgres,
        [framework/ci.md](../framework/ci.md) §1. Nothing here moves it,
        `github`'s included (§9.3).
    *   **The rebuild drill's scratch box**, which keeps
        `state-backend render` and its launch by hand
        ([physical/state-backend.md](../physical/state-backend.md)
        §7.3.1).
    *   **The installation's threat model**, a document of its own
        (rfc-005, proposed in
        [Aetf/kluster#399](https://github.com/Aetf/kluster/pull/399)).
        §3.3 weighs what a committed state publishes against what this
        repository already publishes, and needs nothing from the model.
    *   **A second network interface on the box, or a route table set on
        an interface.** No resource of this stack declares either, so
        Pulumi compares neither, and this document proposes nothing
        about them.
    *   **Deleting the retired security group**, which is done by hand
        after the migration's replacement; slice 6 names the step.

--------------------------------------------------------------------------------

## 1. Context and problem statement

The appliance is the Postgres instance every other stack keeps its state
in, on an OCI `VM.Standard.E2.1.Micro`
([physical/state-backend.md](../physical/state-backend.md)). It is
created and converged by `state-backend provision`, a console script that
models OCI's and B2's APIs one call at a time: a survey that adopts every
entity by display name, a comparison per entity against what the commit
declares, and one write path for every difference it finds — the box's
replacement, with a dump before it and a restore owed after it. So a
drift that never touches the box, a widened security rule or a changed
retention on the dump bucket, is repaired only by replacing the box,
which costs minutes of 5432 downtime and a restore. The comparison is
a large part of the script, and it detects; it does not repair.

The script exists because of one sentence in
[framework/ci.md](../framework/ci.md) §1: the backend must exist before
Pulumi can act, so no stack declares it. That holds for a stack whose
state lives in the backend the stack creates. It does not hold for a
stack whose state lives somewhere else, and a stack is what brings each
entity converging on its own, a diff before every write, and no write
where nothing differs.

Two facts stand in the way, and both are cheap to move now:

*   **The render mints keys on every run.** The server key is random at
    each issuance, the SSH host key is minted per render, and the dump
    key per launch, so the Ignition a declaration would compute is
    different on every run and any declarative tool sees a new box
    every time. That is why the box carries a digest map that code of
    ours compares, and why the kit is on the provision path. Making the
    keys stable (§5) is what lets the diff Pulumi computes be the box's
    bill of materials.
*   **The state needs a home that is not the appliance** (§3.1), and the
    home changes what every run has to do around it (§3.4).

The operator's review of the posted design ruled the approach and
reshaped three things: a state committed to this repository is argued
as a first-class home rather than dismissed; the appliance's directory
under `deploy/` moves into the package; and the two stacks an operator
runs by hand, `github` today and `state-backend` after this, are driven
the same way.

--------------------------------------------------------------------------------

## 2. What is inherited, and what is decided here

Everything in the left column is settled; this document only says where
it lands for the appliance.

| Precedent | Applied here |
| --- | --- |
| rfc-002 §8.1 — every provider explicit, its credential read at the line that builds it ([style/pulumi.md](../style/pulumi.md), "Layering") | the appliance's OCI and B2 providers, built by the stack program from its configuration (§4.1) |
| rfc-002 §7.4 and [framework/pulumi.md](../framework/pulumi.md) §5.2 — a dynamic provider carries no connection state, and reads its credential in `configure` | the image upload and the readiness wait (§4.1) |
| style/pulumi.md — every component and dynamic resource states its type token | the appliance's component and its two dynamic resources (§4.1) |
| rfc-002 §9.1 — a configuration language lives in a file beside the module that renders it, found through `importlib.resources` | the Butane template and the dump script (§8) |
| [framework/github.md](../framework/github.md) §1 — a stack held away from CI by a passphrase no Environment holds | the `state-backend` stack, and the passphrase both stacks share (§6) |
| [credentials.md](../credentials.md) §2 — one acquisition chain for a value a person supplies | how a run finds the operator passphrase (§6) |
| AGENTS.md — a script is Python, and its logic lives in a console script | the one driver for the operator stacks (§9) |
| [framework/testing.md](../framework/testing.md) §5.1 — a run names its own backend on the process it starts | every operator stack's run (§9.2) |
| style/pulumi.md, "Layering" — a script imports no component | the render, the dump, the restore and the wait move into `kluster.lib` (§8) |
| framework/pulumi.md §3.2 — where a pin lives | the appliance's pins, with the exception §8 argues |

--------------------------------------------------------------------------------

## 3. Where the state lives

### 3.1 The constraint

The appliance holds the state of every other stack, and a replacement
leaves it empty until a restore. Its own stack cannot keep its state
there: the run that replaces the box would be deleting the store it is
writing to, and a recovery from total loss starts with no box at all.
So the home is outside the appliance and reachable with no appliance.

A state thrown away after each run does not serve this stack, although
it served a network-only program. B2 returns an application key's secret
once, at creation, so a dump key that no state keeps cannot be carried
to the next box; and OCI does not return an image's source, so an image
imported afresh on every run differs from its declaration and is
replaced every time.

### 3.2 What a `file://` backend writes

Measured with the pinned CLI, 3.257.0, and the locked Python SDK, in a
scratch project on a `file://` backend of its own in the form
framework/testing.md §5.1 gives, with one dynamic resource and one
secret. The source read is the same tag's `pkg/backend/diy/`.

*   **The layout.** Under `<root>/.pulumi/`:
    *   `meta.yaml`, the layout's version;
    *   `stacks/<project>/<stack>.json`, the checkpoint;
    *   next to it, `<stack>.json.bak`, the checkpoint before the last
        write, made on every save by `backupTarget` in `state.go`, with
        no switch;
    *   `history/<project>/<stack>/`, two files for every operation that
        writes — the update's record and a copy of the checkpoint —
        made by `addToHistory` in `state.go`, with no switch;
    *   `backups/<project>/<stack>/`, one more copy per writing
        operation, which `PULUMI_DIY_BACKEND_DISABLE_CHECKPOINT_BACKUPS`
        turns off
        ([environment variables](https://www.pulumi.com/docs/iac/cli/environment-variables/));
    *   `locks/<organization>/<project>/<stack>/<id>.json` while an
        operation runs (`lock.go`);
    *   and beside every one of those a `.attrs` file, which the storage
        library behind the backend writes unless the URL carries
        `?metadata=skip`
        ([`fileblob`](https://pkg.go.dev/gocloud.dev/blob/fileblob)).

    `PULUMI_DIY_BACKEND_RETAIN_CHECKPOINTS` would add a timestamped copy
    of every checkpoint, and is off unless set.
*   **A backend holding only `meta.yaml` and the checkpoint serves every
    operation.** A copy of the directory with everything else removed
    previewed and applied, and wrote the rest back. So the one file that
    has to travel between machines is the checkpoint, and `meta.yaml` is
    a constant.
*   **Which commands write.** `preview`, refreshed or not, writes
    nothing: with the target changed behind the state, the checkpoint's
    digest and the directory's file count were the same after a
    `preview --refresh` as before it. `up`, `refresh`, `import` and the
    `state` commands write.
*   **Every write rewrites the whole file, and an `up` that changes
    nothing still changes it.** The manifest's timestamp moves, and every
    secret is encrypted again under a fresh nonce, so each ciphertext in
    the file is new. A checkpoint's diff after an `up` is never empty,
    and its secret lines always all move.
*   **Every resource records where the program declared it.** Each
    carries a `sourcePosition` and a `stackTrace`, as
    `project:///<path>#<line>` relative to the project root, which here
    is the checkout, and reaching out of it for an interpreter or an SDK
    installed elsewhere (`project:///../.local/share/uv/python/…`).
    Neither the Python SDK (`_get_stack_trace` in
    `pulumi/runtime/resource.py`) nor the engine
    (`sdk/go/common/env/env.go` names no variable for it) can turn this
    off. A run from a checkout laid out differently, or after an edit
    that moves a line, rewrites those entries.
*   **What is encrypted.** A value marked secret is a ciphertext envelope
    under the stack's secrets provider, here its passphrase. Everything
    else is plaintext: "configuration settings, computed URLs, or resource
    identifiers", in Pulumi's own words
    ([secrets](https://www.pulumi.com/docs/iac/concepts/secrets/)). The
    engine carries secretness from an input to the output of the same
    name, on a create or an update and on a refresh's read
    (`annotateSecrets` in `pkg/resource/plugin/provider_plugin.go`); an
    output under another name is plaintext unless the provider or the
    program marks it.
*   **The checkpoint is written as the operation proceeds**: "Pulumi
    records checkpoints early and often as it executes"
    ([state and backends](https://www.pulumi.com/docs/iac/concepts/state-and-backends/)).
    A run that dies leaves the record of every step it finished.

So what a committed state needs from Pulumi is there: one file carries
the stack, the copies are either switched off or never leave the
machine, and nothing else about the backend is specific to where the
directory sits.

### 3.3 What the committed checkpoint publishes

This repository is public, so a committed checkpoint is published, and
**the publication is the push**: the branch that carries a checkpoint is
public from the moment `jj git push` sends it, before any review, and
nothing done to the pull request after that takes it back. What the
appliance's checkpoint would carry, read from the pinned `pulumi-oci`
4.20.0 schema and the committed `pulumi_b2` SDK, against what the
repository publishes today:

| What | Where it comes from | Published today? |
| --- | --- | --- |
| The OCID of every resource: the network's five, the image and its bucket, the instance and its boot volume, the reserved address and the private address it points at | every OCI resource's `id` and the references between them | The tenancy OCID and every compartment OCID are, in `conventions/providers.py` |
| Names, ranges, DNS labels, security rules, shape, availability and fault domain, the box's private address | the declaration, and OCI's answers to it | All but the availability domain's tenancy prefix are, in the component and `settings.py` |
| The reserved public address | `PublicIp.ipAddress` | Yes, as `settings.ADDRESS`, and on 5432, 22 and in the certificate |
| The Object Storage namespace, and B2's bucket id and lifecycle | the image bucket and the dump bucket | The B2 account and both bucket names are |
| When each resource was made and last touched | the engine and OCI | No |
| Paths from the checkout to the interpreter, the SDK and the CLI's language host under mise's install directory | `sourcePosition` and `stackTrace` (§3.2) | The code's own paths are; the rest says the checkout sits in a home directory beside `.local/`, and names the CLI's version, so a pin bump rewrites every resource's entries at the next write |
| Digests of what the box was built from | `extendedMetadata` (§4.1) | The inputs are, or are random keys whose digest tells nothing |
| Plugin versions and the B2 bridge's parameters | the provider resources | Yes, in `uv.lock` and `Pulumi.yaml` |
| **The OCID of the user that made the image bucket** | `Bucket.createdBy` | **No**: the repository keeps a user OCID as a config secret, because it is the identity a key signs as |
| **The appliance user's OCID and its key's fingerprint** | the OCI provider resource's inputs | **No**, as above. The schema marks neither secret; they are ciphertext only because the program passes them from configuration secrets, which the second check below holds |
| **The dump key's id** | `ApplicationKey.applicationKeyId` | **No**: the B2 provider's own configuration marks a key id secret |

**Every plaintext row is an identifier or a fact the declaration already
states, and none of them authorizes anything.** An OCI request is signed
by a principal's private key, and the header naming the signer carries
`<tenancy OCID>/<user OCID>/<key fingerprint>` in the clear by design
([request signatures](https://docs.oracle.com/en-us/iaas/Content/API/Concepts/signingrequests.htm));
a B2 call is authorized by the application key, not its id. The rows in
bold are the ones the repository treats as private elsewhere, and the
stack keeps every one of them ciphertext (§4.1). OCI's default tags,
where a tenancy has them, record the creating principal's *name* in
`definedTags`
([automatic tag defaults](https://docs.oracle.com/en-us/iaas/Content/Tagging/Concepts/understandingautomaticdefaulttags.htm)),
and those names are derived in code and published already.

**The ciphertext rows are the class `Pulumi.<stack>.yaml` already
publishes.** Under the operator passphrase (§6), the checkpoint holds
the Ignition, whose secrets are all configuration secrets too (§5); the
dump key's secret, a key that can only write under one prefix; copies of
the provider credentials the configuration holds; and the dynamic
resources' inert provider records
([framework/pulumi.md](../framework/pulumi.md) §5.2). The passphrase is
32 random bytes, and it already guards, in public, the `github` stack's
admin token, which can switch off the protection on `main`. The one new
kind of value is the dump key, a *generated* secret, which rule 6 of
[credentials.md](../credentials.md) §1 keeps out of committed
configuration because committed ciphertext is public. Committing this
stack's state gives its state the configuration's exposure, and §13 has
that rule say so rather than contradict it.

**What holds this is two checks on the file, not a list of fields.**
They run at the end of every run that wrote the checkpoint, before the
operator can push it, and they are the only gate between a run and the
public: review comes after the push.

*   **No secret value in the clear.** The driver reads the stack's
    secrets in the clear — its configuration's, and its state's through
    `pulumi stack export --show-secrets` — and fails the run if any of
    those strings appears in the file outside a ciphertext envelope,
    whole or line by line for a multi-line value such as a key.
*   **Every property the program declares secret is ciphertext**, which
    needs no value to check. For each resource in the file, every key
    its recorded options name in `additionalSecretOutputs`, and every
    output whose input of the same name the file holds as ciphertext, is
    itself a ciphertext envelope. It holds only what the program
    recorded. A resource that reached the file by a path the program
    did not run carries neither trigger: `pulumi import --file` records
    no secret output names and takes the provider's read as its inputs
    ([`import.go` at 3.257.0](https://github.com/pulumi/pulumi/blob/v3.257.0/pkg/resource/deploy/import.go#L744-L762)),
    so a box imported that way would carry its Ignition's keys in the
    clear past both checks. The next rule closes that path rather than
    checking it.

**New rule** ([framework/pulumi.md](../framework/pulumi.md) §3): every
import into a stack whose state is committed goes through the program,
with the `import_` option on the resource it declares, which carries
the program's secret markings into the imported state (the program
import path of
[`step_generator.go` at 3.257.0](https://github.com/pulumi/pulumi/blob/v3.257.0/pkg/resource/deploy/step_generator.go#L1140-L1176)).
The driver refuses a passed-through `import` for such a stack (§9.2),
and a box another run created is never imported: it is terminated by
hand, as at the cutover (§14, slice 6).

Both name the property they found. A provider release that stops marking
a field, or an output that echoes a secret input under another name,
fails there rather than in a public commit. The second check also runs
in CI over every committed checkpoint, which cannot stop a publication
but makes one loud enough to rotate after.

**A refused check leaves the stack a way on.** The file holds the value
in the working copy and in `jj`'s local snapshots, and nowhere public.
The fix is a program change that marks the property secret, and the
next `up`: marking an output secret on a resource already in the state
rewrites it as ciphertext at the next write, with no other change to
the resource (measured at the pin, with `additional_secret_outputs`
added to a resource an earlier `up` had stored in the clear). That `up`
plans nothing, so the driver records a failed check beside the
checkpoint and runs the next `up` even with nothing planned while the
record stands; a run whose checks pass clears it. The fix is squashed
into the change that carries the leak, so no commit that reaches the
forge holds it.

**Whole-file encryption was weighed and loses.**

*   Encrypted to the escrow's recipients, every run needs the kit, which
    §5 takes off this path.
*   Encrypted under the operator passphrase, it needs no new key, but it
    puts an encrypt and decrypt step of our own around every run, and it
    turns the one thing a committed state offers over a bucket — a state
    diff a reviewer can read in the pull request — into an opaque blob.
    What it hides is the table's plaintext rows, which grant nothing. If
    they must be hidden, the bucket of §3.5 hides them with no scheme of
    our own.

### 3.4 What "every run ends in a commit" means

**The checkpoint is an ordinary file of this repository.** The driver
gives the stack the backend
`file://<checkout>/checkpoints?metadata=skip`, with
`PULUMI_DIY_BACKEND_DISABLE_CHECKPOINT_BACKUPS` set. Of what Pulumi
writes there (§3.2), `checkpoints/.pulumi/meta.yaml` and
`checkpoints/.pulumi/stacks/<project>/<stack>.json` are tracked, and
`.gitignore` names the `.bak` files and the `history/`, `backups/` and
`locks/` directories, which never leave the machine. A run that writes
leaves the checkpoint changed in `@`, as any edit leaves a file changed;
the operator describes the change and pushes it as a pull request like
any other, and the merge puts it on `main`.

**New rule** ([framework/pulumi.md](../framework/pulumi.md) §3): a stack
whose state is committed keeps its checkpoint as a tracked file, a run
of it starts from a working copy that holds the forge's current `main`,
and a run that wrote the checkpoint leaves it for the operator to land
like any change.

The operator's review puts the case for this in four facts, and they
hold: the stack changes a few times a year; no CI job runs it, because
no Environment holds its passphrase; one operator runs it at a time; and
a run ends in a change to commit, like any other edit.

**The primary checkout's routine is what has to change.** A run starts
from the checkout that holds `.credentials/`, the only place the slots
answer (framework/testing.md §5.1), and that checkout's `@` is replaced
with `jj new main` after every fetch
([framework/dispatch.md](../framework/dispatch.md) §2, rule 5). `jj`
snapshots the working copy into `@` on every command, so nothing is
lost, but a change holding an unlanded checkpoint leaves the working
copy, and the next run would start from `main`'s older one.

*   **New rule** (framework/dispatch.md §2, rule 5): what the primary
    workspace's `@` holds is the operator's — most often a checkpoint a
    run left — and after every fetch it is carried onto `main` with
    `jj rebase -b @ -d main`, whatever it holds, and is never replaced
    with `jj new main`. An `@` that holds nothing rebases the same way,
    so there is nothing to check first. This rewrites both the rule's
    headline, "The primary workspace holds no work", and its "restored
    with `jj new main` after each fetch". Measured with `jj` 0.45.1:
    when the forge deletes the merged branch before the fetch, which
    `delete_branch_on_merge` makes the ordinary order, the fetch itself
    abandons the landed change; when the fetch runs first, or the branch
    is kept, the rebase leaves the landed change behind as an empty
    described commit between `main` and `@`. That leftover is noise
    rather than loss, and it is abandoned by hand before it rides into
    the next pushed branch.

**What the driver still checks is what `jj` cannot express**, in this
order:

1.  **A working copy behind the forge's `main`.** Before any command,
    the driver reads the forge's `main` with `git ls-remote`, which
    writes no ref, and runs `git merge-base --is-ancestor` of it against
    `@`. Any non-zero exit is a refusal: 1, the forge's `main` fetched
    but `@` not rebased onto it, names the rebase above; anything else,
    128 when that `main` has not been fetched at all, names
    `jj git fetch` first. A checkout that has not fetched a merged
    checkpoint would otherwise run from the older one.
2.  **A conflicted checkpoint.** Two runs from one base, on two
    workstations, meet as a `jj` conflict in the file when the second is
    rebased onto a `main` carrying the first: each run rewrites the
    manifest's timestamp (§3.2), so the two never merge silently. The
    driver refuses while the file is conflicted and names the
    reconcile: keep `main`'s side (`jj restore --from main <path>`), run
    a refreshed `plan`, and bring in through the program's `import_`
    what the other run created, or delete it by hand; a box the other
    run created is terminated by hand, never imported (§3.3). **The
    ancestry check comes first because of this remedy.** After a
    checkpoint lands, the fetch that abandons the landed change can
    leave a second, unlanded run's checkpoint conflicted in the working
    copy until the rebase resolves it. Checked first, the conflict's
    remedy would throw that unlanded run away; checked second, the
    ancestry refusal sends the operator to the rebase, which resolves
    it.
3.  **A run that changed nothing.** `up` runs only when the refreshed
    preview plans a step (§7), or while a failed check of §3.3 is
    recorded. A writing command whose deployment, read from the
    `--show-secrets` export and without `manifest.time`, is the one it
    started from has the file's previous bytes put back, so no change
    and no pull request come of it; §3.2's re-encryption would otherwise
    cost one per run. That export keeps each secret's envelope beside
    its plaintext, so a file re-encrypted because a property became
    secret counts as a change and keeps its new bytes.

Beside those:

*   **A stack with no checkpoint on `main` yet** starts with `stack init`
    through the driver, which writes the first checkpoint into the
    working copy, landed the same way.
*   **A run that fails part way** has written every step it finished
    (§3.2), and its checkpoint is landed like any other; the pending
    operations Pulumi records for a step in flight are the same under
    every backend, and so is what clears them.

**What no working copy can carry is a checkpoint that has not left
another workstation.** A run on one workstation whose change has not
landed is invisible to a run on another, which plans from `main`'s older
checkpoint and sees what the first created as absent. The one resource
where that matters is the box, and two things stand in the way:

*   **The driver refuses a create of the instance while the refreshed
    reserved address is assigned and the refreshed state holds no
    instance**, with or without `--force` (§7). When the first run
    pointed the address at a box of its own, that is the case: the
    second run's refresh finds its own old box gone and the address
    held. On a first launch, or after the box is lost, the address is
    assigned to nothing: a reserved address outlives the private address
    it was assigned to, which the instance's termination deletes
    ([public IP addresses](https://docs.oracle.com/en-us/iaas/Content/Network/Tasks/managingpublicIPs.htm)).
    In a replacement the state still holds the old instance, so the
    check does not stand in its way.
*   **A restore owed is read from the backend, not from a workstation.**
    The script records one in a workstation slot today, which no other
    workstation reads. Here, a backend that answers and holds no stack
    is read the way the nightly dump reads it when it refuses an archive
    ([physical/state-backend.md](../physical/state-backend.md) §5), and
    that reading has two cases: a box owed a restore, or a site before
    its first `pulumi stack init`, where nothing is owed. The readiness
    hook checks it whenever its run holds no dump of its own, and exits
    3 naming both — `state-backend restore <file>`, or the first
    `stack init` of a new site (§4.3); `plan` exits 3 while the backend
    holds no stack. A backend that does not answer at all is neither:
    `plan` then exits 4, naming the address it dialed. So a second
    workstation that launches a box after the first was killed between
    its terminate and its restore ends at exit 3, not at exit 0 over an
    empty backend.

Two things are left, and named. **A run killed after it created the new
box but before it pointed the address at it** leaves that box unknown
to every other workstation, and a second run launches another beside
it, which is then an orphan to terminate by hand. **The dump a
replacement took** is on the workstation that took it, encrypted, as
today: a restore from another workstation uses it once copied there, or
restores the newest nightly dump and loses what changed since
(ruling 10). The way out of either is to finish on the first
workstation and land its checkpoint.

**What this costs** is a pull request through CI for every run that
wrote, and the driver's checks: an `ls-remote` and an ancestry test, a
conflict test, the byte restore of an unchanged deployment, the two
checks of §3.3 and the record of one that failed. They are tested over a
scratch repository and a scratch backend. That is the committed home's
price, weighed against the bucket's in §3.5.

### 3.5 Against B2, and the other homes

The alternative the posted design recommended is Pulumi's S3 backend on
B2, in a bucket of its own. Pulumi's DIY backend takes an S3-compatible
server through an `endpoint` parameter
([DIY backend](https://www.pulumi.com/docs/iac/operations/stack-management/using-a-diy-backend/)),
B2 offers one with an application key as the key pair
([B2's S3-compatible API](https://www.backblaze.com/apidocs/introduction-to-the-s3-compatible-api)),
and a key confined to one bucket also needs `listAllBucketNames` for S3
SDKs
([B2 S3 app keys](https://www.backblaze.com/docs/cloud-storage-s3-compatible-app-keys)).

| | Committed to this repository (recommended) | B2, through the S3 backend |
| --- | --- | --- |
| Credentials a run needs besides the passphrase | none: the operator pushes the change as any other, with the access they push everything with | a B2 key confined to the state bucket, in a workstation slot, found through §6's chain |
| What exists outside any stack | nothing | the state bucket, made by the command that mints its key, since it cannot be a resource of the stack whose state it holds |
| A third party's compatibility on the bootstrap path | none: the file backend is the CLI's own | Pulumi's writes to S3-compatible servers broke twice this summer ([IBM COS](https://github.com/pulumi/pulumi/issues/23764) from 3.248; [R2](https://github.com/pulumi/pulumi/issues/24219) on 3.256.0, [fixed](https://github.com/pulumi/pulumi/pull/24292) after 3.257.0, the pin). Nothing settles B2 at the pin without a spike, and every CLI bump's review owes a `plan` against B2 from a workstation, since CI cannot hold the key |
| Two runs at once | a `jj` conflict in the file, which the driver refuses to run over (§3.4) | the DIY backend's lock file in the bucket |
| A stale checkpoint | the checks of §3.4, with the two cases it names left over | none: every run reads the one checkpoint |
| What is published | the plaintext rows of §3.3, and ciphertext of the class the configuration already publishes, at the push | nothing |
| A corrupted checkpoint | every checkpoint that ever landed is in `main`'s history | the bucket's prior versions, until a lifecycle rule expires them |
| A lost checkpoint | needs every clone gone | every resource imported again by id, and the dump key, whose secret B2 returned once, replaced with the box |
| What review sees | the state's diff in the pull request, with every ciphertext line moved, after the push has published it | nothing |
| Ceremony per run that writes | a pull request through CI, and a merge; a run that changes nothing leaves the file as it was (§3.4) | none |

**Recommended: committed.** The operator's four facts are the reason the
ceremony is cheap: a few runs a year that write, each by the one
operator, each already a deliberate act. Against that, the bucket costs
a credential and its slot, a bucket that no stack declares, a spike
before anything can be built, a third party's compatibility on the path
every recovery takes, and a lost state that is far likelier than a
repository lost with every clone. The bucket stays the answer if the
operator judges §3.3's plaintext rows unfit to publish, or a pull
request per run too heavy (ruling 1).

The other homes, each rejected on one ground:

*   **Pulumi Cloud's free tier** — one user, unlimited stacks and
    history ([pricing](https://www.pulumi.com/pricing/)) — adds an
    account, which is a root in [credentials.md](../credentials.md) §2
    with its own succession, and hands the one bootstrap-critical state
    to a party the installation has no other stake in, where
    framework/ci.md §1 keeps every other state self-hosted. It is the
    bucket's fallback if the bucket's spike fails.
*   **A second small backend** is a second appliance with the same
    question one level down; one at home re-couples the bootstrap to the
    home uplink, which is why the backend moved (framework/ci.md §1).
*   **A `file://` directory synced to a bucket by code of ours** is the
    committed home's mechanism with the bucket's credential, and none of
    the committed home's review.

### 3.6 What a lost, stale or corrupt state costs

*   **Stale**: §3.4 refuses to start from it, but for the two cases it
    names.
*   **Corrupt**: the previous checkpoint is in `main`'s history. Reverted
    by a commit, then a refreshed `plan` shows what the revert does not
    know about.
*   **Lost**: only with every clone of the repository. Then every
    resource but the instance is imported through the program, as the
    cutover imports it (§14, slice 6): the network's kinds, `PublicIp`,
    `Image` and the Object Storage bucket (their Import sections in the
    pinned schema), and the dump bucket (the provider declares an
    importer). The instance is not imported: an import runs no program,
    so the box's `metadata`, with the keys its Ignition carries, would
    be recorded in the clear (§3.3). It is dumped, terminated by hand and
    launched again, and the dump key with it, since an imported
    `ApplicationKey` has no secret: **a lost state costs one
    replacement**, with its dump and restore. The configuration is
    committed, and the passphrase is escrowed.

--------------------------------------------------------------------------------

## 4. The appliance in Pulumi

**The approach is ruled: the whole appliance is one stack**, `state-backend`,
in this repository's one project, declared by a component in
`kluster.components.state_backend` and dispatched by `kluster.main` like
every other stack. Two alternatives were weighed on the decision issue
before it:

*   **A planner per entity inside the script** keeps code of ours that
    models OCI's and B2's APIs — survey, compare, write, wait — and adds
    an executor to order the writes. Every new kind of entity is a
    planner, a fake of its API and their tests.
*   **The network in Pulumi with a state thrown away, the box in the
    script** runs two engines in one command, joined by the subnet's id
    crossing from one to the other on the bootstrap path, with two
    comparisons in one report, two exit semantics merged, and three
    write phases each with its own failure analysis.

One stack puts all of it in one engine, and what the engine does not
cover is two small dynamic resources and two hooks.

### 4.1 Every entity, and what declares it

| Entity | Declared as | What matters |
| --- | --- | --- |
| The network: VCN, internet gateway, route table, subnet | `oci.core.Vcn`, `InternetGateway`, `RouteTable`, `Subnet` | The VCN and the subnet carry `protect=True`: a deployment that would replace either is an error, in a preview too ([protect](https://www.pulumi.com/docs/iac/concepts/options/protect/)). The VCN is declared through `cidr_blocks`, which updates in place. The route table is the program's own. |
| Security rules | One `oci.core.SecurityList` the program owns, carried alone by the subnet; the security group is retired | The rules are one input of one resource, a set in the provider, so a rule added by hand is part of that resource's refreshed state and a difference on the next run. Under a group each rule is a resource of its own, and one nobody declared is in no state. The declared set is TCP 22 and 5432 from anywhere, the ICMP rules of the VCN's default list, and all egress, stateful. |
| The box's interface | `create_vnic_details` on the instance: the subnet, no public address, `nsg_ids=[]` | `nsgIds` updates in place, so a group put on the interface by hand is repaired without a replacement. |
| Custom image | A dynamic resource that fetches the pinned Fedora CoreOS release's `oraclecloud` artifact, checks it against the stream's digest, decompresses it and uploads it to the image bucket; then `oci.core.Image` imports that object | Fedora CoreOS publishes the artifact compressed and OCI imports it only uncompressed; `kluster.providers.talos_factory` fetches and decompresses an image the same way. `imageSourceDetails` replaces on change, so a release bump imports a new image and changes nothing else (ruling 7). |
| Image bucket | `oci.objectstorage.Bucket`, with `additional_secret_outputs=['createdBy']` | Imported at the cutover. `createdBy` is a user OCID (§3.3). |
| Instance | `oci.core.Instance` | `replace_on_changes=['metadata']`, `delete_before_replace=True`, `ignore_changes=['sourceDetails.sourceId']`, `metadata` secret, and the `before_delete` hook (§4.3). `metadata` carries the Ignition as `user_data`. `extendedMetadata` carries a digest per component of what the box is built from, plaintext and updated in place, so a planned replacement names what moved beside a `metadata` the diff can show only as secret. |
| Reserved address | `oci.core.PublicIp`, `RESERVED`, its `private_ip_id` the box's primary private address | `protect=True`. `privateIpId` updates in place, so pointing the address at a new box is an ordinary update. The component refuses a reservation whose address is not `settings.ADDRESS`. |
| Readiness | A dynamic resource whose create waits until the reserved address completes a TLS handshake on 5432 with a certificate that chains to the CA and names the address | Its inputs are the address, the CA certificate and the instance's id, with `replace_on_changes` on the id, so it is replaced with the box (§4.3); it needs no credential, since the box sends its certificate before anything authenticates (physical/state-backend.md §6). Its `after_create` hook restores (§4.3). |
| Dump bucket and retention | `b2.Bucket` with its lifecycle rule | `protect=True`. The rule updates in place in both directions (ruling 8), in the shape `components/backup` declares. |
| Dump key | `b2.ApplicationKey`: `writeFiles`, the dump bucket, the dump prefix; `additional_secret_outputs=['applicationKeyId']` | Its secret is secret in the SDK and kept in state; the program adds its id (§3.3). **Rotation is a committed edit**: the key's name carries a generation from the configuration, a new generation replaces the key, and that rebuilds the box, which must carry the new one. The old key is deleted at the end of that deployment, once the new box holds its successor. A key deleted by hand is gone from the refreshed state, so the next run plans a new one and the box's replacement. |
| Providers | An explicit `oci.Provider` and `b2.Provider`, built by the stack program from the stack's configuration | The OCI key moves from its workstation slot into configuration, and B2 gets a management key of the stack's own (§5). The upload's dynamic provider reads the OCI key in `configure`, as framework/pulumi.md §5.2 prescribes. |

The component's type token and the two dynamic resources' follow
style/pulumi.md's forms, `kluster:<area>:<Type>` and
`pulumi-python:dynamic/<area>:<Type>`.

**New rule** ([style/pulumi.md](../style/pulumi.md), "Resources and
their contents"): in a stack whose state is committed, an output that
identifies a principal, or names a credential by its id, is declared
secret with `additional_secret_outputs` wherever the provider does not
mark it. §3.3's check is the backstop that catches the one missed, not
the mechanism.

### 4.2 The instance's options, and the traps they exist for

Each read from the pinned `pulumi-oci` 4.20.0 schema:

*   **`metadata` updates in place, and is not marked secret.** Left
    alone, a changed Ignition would be written into a running box's
    metadata, which Ignition never reads again.
    `replace_on_changes=['metadata']` makes it the replacement it is,
    and wrapping it secret keeps the keys it carries out of the
    checkpoint's plaintext; the engine carries that to the output of the
    same name (§3.2).
*   **`sourceDetails.sourceId` updates in place too.** The OCI provider
    sends it as an `UpdateInstance` carrying
    `UpdateInstanceSourceViaImageDetails`
    ([`core_instance_resource.go`](https://github.com/oracle/terraform-provider-oci/blob/master/internal/service/core/core_instance_resource.go)),
    which replaces the running box's boot volume, data directory
    included, **without deleting the instance**, so without the dump
    hook. `ignore_changes=['sourceDetails.sourceId']` is what stops an
    image bump from doing that. The path is that one field and not the
    whole block, so the boot volume's size still converges. Zincati
    keeps the running OS current, and the image matters only at the
    next launch.
*   **Every declared input the schema marks as replacing** —
    `availabilityDomain`, `createVnicDetails.subnetId` and
    `createVnicDetails.privateIp` among them — passes through the same
    gate and hook as a changed Ignition, since the gate reads the
    replacement and not its cause.

**New rule** ([style/pulumi.md](../style/pulumi.md), "Resources and
their contents"): an input that the provider updates in place, but
that the target reads only when it is created, such as boot
configuration or a boot image, is never left to be updated in place.
It is declared `replace_on_changes` where a change must rebuild the target, or
`ignore_changes` where another owner keeps the running target current,
and a test holds each such option on the resource.

### 4.3 The replacement: dump, replace, restore

The native options of §4.2 do the replacement. A `before_delete` hook on
the instance owns the dump and the permission; an `after_create` hook on
the readiness resource owns the restore
([resource hooks](https://www.pulumi.com/docs/iac/concepts/options/hooks/),
in the engine and the Python SDK since 3.182.0).

*   **The dump.** The hook runs whenever the engine deletes the
    instance, the delete half of a replacement included: `DeleteStep`
    runs the old state's `before_delete` hooks before the delete
    ([`step.go` at 3.257.0](https://github.com/pulumi/pulumi/blob/v3.257.0/pkg/resource/deploy/step.go#L626-L640)).
    A before hook that fails leaves its action unexecuted and fails the
    deployment, so a failed dump leaves the old box serving, which is
    the guarantee physical/state-backend.md §1 makes today. Under
    `delete_before_replace` the old box is deleted before the new one is
    created, so the dump reaches the old box through the reserved
    address, which still points at it. The hook writes the encrypted
    dump where the script writes it today, and keeps the plaintext in a
    temporary directory of the program's process for the restore. It
    writes no record of a restore owed: the empty backend is that record
    (§3.4).
*   **The permission.** The delete hook refuses before it dumps unless the
    run carries the replacement permission, which the driver sets for
    `--force` and `--replace` (§7), and a `before_create` hook on the
    instance refuses the same way (`CreateStep` runs it,
    [`step.go` at 3.257.0](https://github.com/pulumi/pulumi/blob/v3.257.0/pkg/resource/deploy/step.go#L321-L325)).
    That is the gate the engine enforces: any `up` that would create,
    replace or delete the instance, however it was started, fails at
    that step with the box untouched, and names `--force`. The first
    launch is a create, and asks for `--force` like any other.
    *   **New rule** ([framework/pulumi.md](../framework/pulumi.md)
        §3): a value that belongs to one invocation, such as permission
        for a destructive step, reaches the program as an environment
        variable the driver sets on the process it starts. It is read
        where it is acted on, and never from stack configuration, which
        is committed.
*   **Preview runs no hook.** A hook is skipped on a dry run unless
    it is registered for one
    ([`deployment.go` at 3.257.0](https://github.com/pulumi/pulumi/blob/v3.257.0/pkg/resource/deploy/deployment.go#L849-L861)).
*   **The restore.** The readiness resource is created once the new box
    answers on the reserved address, and it declares
    `replace_on_changes` on its instance-id input, so a new box replaces
    it rather than updating it: a dynamic provider with no `diff` of its
    own answers that it does not know, and the engine then updates the
    resource, which runs no `after_create`. Its `after_create` hook
    restores the dump from this run's plaintext and verifies it with
    `pulumi stack ls` as `state-backend restore` does
    (physical/state-backend.md §7).
    *   An after hook that fails leaves its resource recorded and the
        deployment failed
        ([`step.go` at 3.257.0](https://github.com/pulumi/pulumi/blob/v3.257.0/pkg/resource/deploy/step.go#L497-L503);
        the Python SDK's `ResourceHookBinding` docstring says otherwise,
        which is why the engine test holds it). The backend still holds
        no stack, and the run exits 3.
    *   A run whose box is new but which took no dump itself — a first
        launch, a launch after a lost box, the cutover, or a run after
        one that died between its terminate and its restore — finds a
        backend that holds no stack and no plaintext to restore from. The
        hook names `state-backend restore <file>`, or the first
        `stack init` where the site has no state yet, and the run exits
        3 (ruling 4).

*   **The hooks' credential** is the `operator` client bundle, which
    authenticates to the estate's backend rather than to any provider
    of this stack. The stack program reads it from its workstation slot
    and passes it down to the component as a parameter, and the hooks
    receive it from there.
    *   **New rule** (rule 6 of [credentials.md](../credentials.md) §1,
        and [style/pulumi.md](../style/pulumi.md), "Layering"): a
        credential that authenticates a stack's program to the estate's
        own state backend, rather than a provider of that stack, is read
        by the stack program from its workstation slot and passed down
        as a parameter. It is not copied into configuration: the slot is
        its one copy per workstation, reissued by `state-backend bundle`,
        and a configuration copy would put the `operator` key into a
        committed file for every workstation to carry. §12 names the
        exception.

**Why not the alternatives:**

*   **A dynamic resource whose `delete` dumps** runs before the
    instance's delete only if the engine counts it among the dependents
    a delete-before-replace removes first, a computation over its `diff`
    with the instance's id unknown. Where that goes the other way, the
    dump runs after the terminate, against the new and empty box. The
    hook is bound to the delete itself.
*   **Two phases, the driver dumping and then running `up`**, ties the
    dump to nothing: a `pulumi up` by hand replaces with no dump, and a
    plan that moved between the preview and the `up` replaces what
    nobody dumped for.
*   **`protect` as the gate** refuses a replacement only when the old
    state *and* the program both protect the resource
    ([`step_generator.go` at 3.257.0](https://github.com/pulumi/pulumi/blob/v3.257.0/pkg/resource/deploy/step_generator.go#L1954-L1972)).
    A run that removes the protection records the new box unprotected,
    and the next plain run would replace it without asking. `protect`
    stays for what no run may replace: the VCN, the subnet, the reserved
    address and both buckets.

**An engine test holds the semantics this rests on.** It runs the pinned
`pulumi` against a `file://` backend in the test's own directory, with a
stand-in dynamic resource in the instance's place and a dependent one in
the readiness resource's, and holds each of these:

*   a raising `before_delete` hook leaves the stand-in undeleted and
    fails the run, and a raising `before_create` hook leaves it
    uncreated;
*   a passing `before_delete` hook runs before the stand-in's delete;
*   a preview runs no hook;
*   a raising `after_create` hook leaves its resource recorded and fails
    the run;
*   a replacement is created from the program's inputs, not from the old
    values an `ignore_changes` kept: a stand-in replaced for one input
    is created with the program's current value of an ignored one
    ([`step_generator.go` at 3.257.0](https://github.com/pulumi/pulumi/blob/v3.257.0/pkg/resource/deploy/step_generator.go#L2003-L2023),
    and for a targeted replacement
    [L1321–L1331](https://github.com/pulumi/pulumi/blob/v3.257.0/pkg/resource/deploy/step_generator.go#L1321-L1331)).
    That is what makes §4.2's "the image matters only at the next
    launch" true: if the engine behaved otherwise, a replacement forced
    after a release bump would launch from the image the `Image` replacement had
    already deleted;
*   the dependent, declared `replace_on_changes` on the stand-in's id, is
    replaced when the stand-in is, and its `after_create` hook runs.

**New rule** ([framework/testing.md](../framework/testing.md)): an
engine semantic a design depends on and no mock can show is pinned by an
engine test of that kind. It runs in the suite under the same timeout,
with its own backend and home named on the process, as they are for a
scratch probe (framework/testing.md §5.1).

### 4.4 Why the render becomes a function of the commit

The render mints three keys today: a server key at each issuance, an
SSH host key per render, and a dump key per launch. So the declared
`metadata` would differ on every run, and the box carries a digest map
that code of ours compares instead, with the certificate compared by
what it asserts and its expiry against a clock
([physical/state-backend.md](../physical/state-backend.md) §1).

With the three keys stable (§5), the render reads the commit, the
configuration and the state, and nothing else. A test holds two renders
from the same inputs equal byte for byte, and the diff Pulumi computes
on `metadata` is then the bill of materials. The digests survive in
`extendedMetadata` to say which component moved; nothing compares them
but the engine, and they trigger nothing. A comment-only edit of the
Butane template changes no rendered byte, so it replaces nothing and
moves one digest in place, where today it is drift that `--force`
answers.

### 4.5 What stays outside Pulumi

*   **`state-backend dump` and `restore`**, the playbooks' standalone
    moves (physical/state-backend.md §7), and the restore with the kit
    that a failed run leaves. Their code moves into `kluster.lib` (§8),
    so the hooks run the same code.
*   **`render`** (for the scratch box of physical/state-backend.md
    §7.3.1), **`ssh`**, **`pins`**, **`bundle`** and **`probe`**: none
    of them creates or
    changes what the stack declares. `ssh` pins the host key's committed
    public half (§5) and makes no OCI call.
*   **The `credentials` commands that fill the stack's configuration**
    (§5), as for every other stack.

### 4.6 What this deliberately does not do

*   Run in CI: no Environment holds the operator passphrase (§6).
*   Restore without the kit a dump that an earlier run took.
*   Take a lock across workstations beyond what the rebase of a
    checkpoint, and the conflict it raises, give (§3.4).
*   Refuse to run a checkout whose code differs from `main`. The run
    applies the checkout it runs in, as a `pulumi` run always has; what
    it publishes is the checkpoint, never the code.

--------------------------------------------------------------------------------

## 5. Stable keys

| Key | Minted by | Held in | Rotated by | Needs the kit |
| --- | --- | --- | --- | --- |
| Server TLS key and certificate | `credentials derived state-backend-server issue` (new), from the escrowed CA | Configuration: the key as a secret; the certificate and the CA's certificate in the clear | Issuing again, then the replacement `--force` asks for | At issuance only |
| SSH host key | `credentials derived state-backend-host-key generate` (new) | Configuration, as a secret; its public half committed beside the Butane template (§8) | Generating again, then the replacement | No |
| Dump key | the stack's `b2.ApplicationKey` | State, as a secret | A generation bump in configuration | No |
| Age recipients, the public halves | `credentials derived backup-age-<N> generate`, which writes them beside the drill recipient (§8) | Committed | The generation pin (physical/state-backend.md §7.4) | At generation only |
| OCI API key for the appliance | `credentials derived oci-state-backend mint`, retargeted from its slot | Configuration, as a secret | Minting again | At mint only |
| B2 management key for the appliance (new) | `credentials derived b2-state-backend-management mint` (new), from the B2 seed | Configuration, as a secret | Minting again | At mint only |

*   **Commands, not `pulumi_tls` resources.** The server certificate
    needs the CA's key. Issued in the program, that key would be an
    input of the stack, and physical/state-backend.md §3 keeps it out of
    every Pulumi state and every copy but the escrow. The command keeps
    the CA where it is and costs the kit once per certificate, whose
    validity is three years. It adds no provider dependency either. The
    host key takes the same shape, so one pattern covers both (ruling
    5).
*   **What changes in exposure.** The server key and the host key become
    ciphertext in the committed `Pulumi.state-backend.yaml`, under the
    operator passphrase. Today each lives only in its render and in its
    box's `user_data`, and dies with that box. Under this design the
    same key stays on every box until someone rotates it. No holder is
    added but that ciphertext, and the passphrase is generated and
    escrowed.
*   **What a run needs, and nothing more**: the operator passphrase,
    which opens the configuration and the state's secrets; the `operator`
    client bundle already in its slot, which the hooks' dump and restore
    and the check for a restore owed connect with (§4.3); and the
    operator's own push access, to land the checkpoint like any change
    (§3.4).
*   **The kit leaves the provision path.** Today `provision` opens it for
    the CA and the age recipients, whose public halves it derives from
    the escrowed identities, and for the B2 seed, to mint the dump key.
    After this the kit is for minting or rotating a row above, for a new
    workstation's `operator-passphrase recover` and `bundle operator`,
    and for a restore the run did not dump itself.
*   **The run no longer writes the client bundle**: issuing one needs
    the CA, and a bundle outlives many runs.

--------------------------------------------------------------------------------

## 6. The operator passphrase

Beside the Pulumi stack passphrase, which every stack but `github` is
under and every CI Environment holds, the register holds the `github`
stack's own, which opens that stack's configuration and reaches no
Environment ([framework/github.md](../framework/github.md) §1).

**The `github` stack's passphrase is renamed the operator passphrase**,
and covers the `state-backend` stack too. What sets it apart is who
holds it, the operator and never CI, and the repository already uses
`operator` for that side against `ci`: the `operator` role and client
bundle, and the operator keys. So the word carries the property instead
of naming one stack (ruling 6).

*   **What it covers is a definition, not a list**: the configuration of
    every operator stack (§9.1), and the state of every operator stack
    whose state is committed. Today that is `github`'s configuration and
    `state-backend`'s configuration and state.
*   **A run finds it through the account roots' chain**
    ([credentials.md](../credentials.md) §2), first hit wins: the
    desktop secret store; the slot `.credentials/operator.passphrase`;
    the variable `KLUSTER_OPERATOR_PASSPHRASE`; a prompt. The escrow
    stays the recovery path: `credentials derived operator-passphrase
    recover` writes the store or the slot.
    *   **New rule** (rule 6 of [credentials.md](../credentials.md) §1):
        the desktop secret store joins the closed set of storage
        channels, for a value the operator supplies to a run by hand,
        reached through the chain of credentials.md §2. Today that value
        is the operator passphrase. This reverses, for that passphrase
        alone, the rule's "Deliberately not the desktop secret store",
        whose reason is that a `mise` template reads every passphrase on
        every `pulumi` run and can neither prompt nor unlock a keyring.
        The driver reads this one, not a template (§9.2). The stack
        passphrase keeps the old reason and the old channel, since
        `mise` still templates it.
*   **What moves in the register**, [credentials.md](../credentials.md)
    §3: the `github` stack passphrase row becomes the operator
    passphrase, escrowed as `operator/passphrase`, decrypting the
    configuration of both operator stacks and the committed state of
    one; the stack passphrase's scope reads "every stack but the
    operator stacks"; the appliance's OCI key moves from a workstation
    slot to a configuration secret of `state-backend`; the dump key is
    minted through the stack and held in its state and on the box; the
    server key is issued into configuration; and the B2 management key
    and the host key are new rows.

--------------------------------------------------------------------------------

## 7. A run of the `state-backend` stack

What §9's driver does for this stack beyond what it does for every
operator stack:

1.  **Planning**: `plan` runs the refreshed preview and prints it as
    Pulumi does. The refresh is what reads a hand edit: with a persistent
    state, an `up` alone compares the program against the recorded state.
    It writes nothing (§3.2), and exits 1 when anything is planned, 0 when
    nothing is, 3 while the backend answers and holds no stack — a
    restore owed, or a site before its first `stack init` — and 4 when
    the backend does not answer (§3.4).
2.  **A pending replacement is read from the preview's step events**: a
    create, a replacement or a delete of the instance.
    *   `up` with nothing planned leaves the state as it was and exits
        as `plan` does, unless a failed check of §3.3 is recorded, when
        it runs `up` so that the next write re-marks the file.
    *   `up` with steps planned and none of them the instance's runs
        `up` with a refresh, which converges everything in place, and
        exits 0, or 3 while the backend holds no stack.
    *   `up` with one of them the instance's writes nothing, names the
        digests that moved and `--force`, and exits 1 (ruling 3).
    *   `up --force` runs `up` with the replacement permission set;
        `--replace` does the same and adds the instance's URN to
        `replace`.
    *   Either refuses, `--force` or not, a create of the instance while
        the refreshed reserved address is assigned and the refreshed
        state holds no instance (§3.4).
3.  **The server certificate's expiry is a stack output.** The driver
    compares it with the renewal margin and names the command that
    reissues it (§5); the probe stays the backstop.

**Actuations avoided**: an entity that matches is no step. The image
moves only on a release bump, the dump key only on its generation, and
the wait and the restore only with a new box.

--------------------------------------------------------------------------------

## 8. `deploy/state-backend/` absorbed into `src/`

What the directory holds today, and where each piece goes:

| Today in `deploy/state-backend/` | What it is | Where it goes |
| --- | --- | --- |
| The Butane template, `butane.yaml.j2` | The machine, whole | `src/kluster/lib/state_backend/machine/butane.yaml.j2`, rendered through `kluster.lib.templates` |
| The dump script, `state-dump.sh` | What the box's timer runs | Beside it, copied byte for byte into the template. It stays shell, for the reason AGENTS.md admits: the box has no interpreter |
| The operator keys, `operator-keys.txt` | Who may log in for diagnosis | Beside it, a hand-edited list as now |
| The drill recipient, `drill-recipient.txt` | The drill identity's public half | Beside it, written by `credentials derived drill-age-identity generate` as now |
| `README.md` | How to operate the appliance | Into physical/state-backend.md, whose §7 is where its playbooks already live; the rest of the operating form is §7 and §9 of this document once built |

Two files §5 adds join them: the host key's public half and the age
recipients. `deploy/` keeps `cloud-session/` alone.

*   **Why `kluster.lib` and not the component.** The component renders
    the Ignition for the stack, and the `render` command renders it for
    the scratch box, and a script imports no component (style/pulumi.md,
    "Layering"). The same holds for the dump and restore the hooks and
    the `dump` and `restore` commands both run, and for the readiness
    wait. So the code both run, and the files that code reads, live in
    `kluster.lib.state_backend`.
    *   **New rule** ([style/pulumi.md](../style/pulumi.md),
        "Layering"): code that an area's component and a script both run
        lives in `kluster.lib.<area>`, with the files it reads beside it.
*   **What the moved code imports goes with it, or stays behind.** The
    render, the dump and restore and the wait import the `credentials`
    script package today, and nothing in the layering contract forbids
    `kluster.lib` from doing the same, since `kluster.scripts` is no
    layer: the component's hooks would pull the credentials command
    line into the program with every check green. So:
    *   the render takes its keys and recipients as arguments, so the
        escrow and CA code it calls today (`escrow`, `pki`, `age`) stays
        with the script that recovers or mints them and passes them in;
    *   what the dump and restore use of `age` — the binary's name and
        the armor's marker — and of `lifecycle` — the client bundle's
        layout in its slot — moves to `kluster.lib`, beside
        `workstation`, and the `credentials` modules import it from
        there;
    *   the `stack ls` check the restore runs goes through the `pulumi`
        runner that `pulumi_config` holds today, which moves to
        `kluster.lib` for the driver to use too (§9.4);
    *   the kit-opening half of `restore`, which opens the escrow for an
        age identity, stays in the script: the hooks restore from their
        own plaintext and never open the kit;
    *   what the moved code reaches `credentials.workstation` for — the
        checkout, and a slot's directory — is `kluster.lib.workstation`'s
        already, which that module re-exports;
    *   the wait takes the address and the CA certificate and needs no
        OCI client, so `oci_slot` stays with the script until slice 6
        removes the code that uses it.
    *   **New rule** ([style/pulumi.md](../style/pulumi.md),
        "Layering"): nothing in `kluster.stacks`, `kluster.components`,
        `kluster.providers`, `kluster.lib` or `kluster.conventions`
        imports `kluster.scripts`. A forbidden contract in
        `pyproject.toml` holds it, so the slice that lands it owns that
        serialized file.
*   **What the move removes.** The template is rendered today through a
    loader of its own over a directory found from the checkout
    (`DEPLOY_DIR`), because the directory lived outside the package.
    Inside it, the template is found through `importlib.resources` like
    every other rendered file (rfc-002 §9.1), and the package renders
    without a checkout. The commands that write a committed file into
    that directory — the drill recipient, the host key's public half,
    the age recipients — still write it in the checkout they run from.
*   **The pins.** `settings.py` holds what the appliance is pinned to:
    the Fedora CoreOS stream and release, the Postgres image, `age`'s
    version and digest. framework/pulumi.md §3.2 puts every pin a stack
    program reads in `Pulumi.yaml`'s `versions:` block, read through
    `lib/versions.py`, which reads it through the Pulumi SDK and so only
    inside a program. These pins are read by the stack program and by
    scripts that run outside one (`render`, `pins`, `probe`), so one
    copy in project configuration would leave the scripts without it.
    The module moves to `kluster.lib.state_backend.settings`.
    *   **New rule** ([framework/pulumi.md](../framework/pulumi.md)
        §3.2): a pin that a stack program and a script both read lives in
        the `kluster.lib` module they share, not in project configuration.
        The alternative, a reader of `Pulumi.yaml` that scripts can use,
        is the second option of ruling 9.
*   **What else names the directory**: renovate's rule grouping the
    appliance's pins, the auto-merge workflow's comment on paths no
    preview reads, the root README's layout, and framework/ci.md §1.
    Each moves with the files.

**The directory holds no driver.** The appliance's driver has been the
`state-backend` console script since it was written. The one driver in
this repository that is not a console script is `mise.toml`'s `github`
task, and §9 replaces it and `state-backend provision` together with one
console script.

--------------------------------------------------------------------------------

## 9. One driver for the operator stacks

### 9.1 What an operator stack is

**An operator stack is a stack no CI job runs**, which is why its
configuration is under the operator passphrase and why a run of it
starts from a workstation. The census of them is one entry in
`conventions` beside the stack names, recording for each where its state
lives; today it holds `github`, whose state is in the appliance's
backend, and `state-backend`, whose state is committed. Everything that
has to know which stacks are held away from CI reads that census: the
passphrase's scope (§6), the census over the workflows that keeps any
`pulumi` command in CI off them, and the driver.

### 9.2 What every run shares

*   **The environment is the driver's, set on the process it starts**:
    the stack's backend and the operator passphrase. Neither reaches an
    operator stack through `mise.toml`'s `[env]`.
    *   **New rule** ([framework/pulumi.md](../framework/pulumi.md)
        §3): an operator stack runs only through the one driver, which
        sets its backend and its passphrase on the process it starts.
*   **The stack is the driver's argument, and never a flag it passes
    through**: an argument naming a stack of its own is refused.
*   **A run refuses inside a `jj` workspace under `.claude/`**, where the
    slots do not answer.
*   **`plan` is a refreshed preview**, exiting 0 when nothing is planned
    and 1 when something is, which is also how drift in an operator
    stack is read, since no weekly job reads it (framework/ci.md §3).
*   **`up` refreshes, previews, applies the stack's gate, asks, and
    applies**, and `--yes` skips the question.
*   **Anything else is passed to `pulumi`** under the same environment:
    `config`, `import`, `state`, `stack`, `cancel`. An `up` passed
    through this way skips the driver's gate but not the engine's
    (§4.3). An `import` passed through for a stack whose state is
    committed is refused: it would record what it imports without the
    program's secret markings (§3.3).
*   **A stack whose state is committed** gets the checks of §3.3 and
    §3.4 around every command, so a `state` command that wrote the
    checkpoint is checked, and left for the operator to land, just as an
    `up` that wrote it is.

### 9.3 What each stack keeps of its own

| Stack | Its state | Its gate | What a run needs besides the passphrase |
| --- | --- | --- | --- |
| The forge, `github` | the appliance's backend, reached with the `operator` client bundle | none | the client bundle |
| The appliance, `state-backend` | committed (§3.4) | a create, replacement or delete of the instance waits for `--force`, a create while the address is assigned and the state holds no instance is refused, and a backend with no stack exits 3 (§7) | the client bundle, which the hooks and the restore-owed check use (§4.3) |

**`github`'s state stays where it is.** It has a home that exists before
it runs, which is the whole of what the appliance's state lacks, and
moving it would publish the forge's graph for nothing.

### 9.4 The command

One console script, named for the stacks it serves (ruling 2):

    operator-stack github plan
    operator-stack github up
    operator-stack state-backend up --force
    operator-stack github pulumi config get githubAdminToken

It replaces `mise run github` and `state-backend provision`. The
`state-backend` script keeps `ssh`, `dump`, `restore`, `render`,
`bundle`, `pins` and `probe`.

**The stack-to-environment mapping moves into `kluster.lib`**, beside
`workstation`, with the `pulumi` runner of §8: the driver and the
`credentials` commands that write an operator stack's configuration both
reach every stack through it, and `pulumi config set` against a
committed-state stack needs the committed backend as much as an `up`
does.

### 9.5 Why a console script, and not the `mise` task

The `github` task is shell because all it did was check and rearrange
words before `exec pulumi`, and a Python entry point would start the
virtual environment ahead of every run (its comment in `mise.toml`
says so). The driver now chooses a backend per stack, resolves a
passphrase through a chain with a secret store in it, reads a preview's
step events for a gate, and checks a checkpoint's ancestry, conflicts
and secrets. That is logic,
and AGENTS.md puts logic in a console script, with `mise` tasks at most
a convenience on top. The virtual environment's start is paid once per
run, beside a `pulumi` run that starts one for the program anyway.

--------------------------------------------------------------------------------

## 10. What it costs, and what it removes

Counted on `main` today: `provision.py` is 1,693 lines, of which
everything but `ssh` and its pin checks goes; the orchestration of a
provision run in `cli.py`, from the reasons for a rebuild to the
restore owed, goes; from `config.py`, the escrow's roots, the comparison
and the renewal clock; from the credentials' `b2.py`, the dump bucket
and the dump key. Most of `tests/test_provision.py`'s 4,866 lines test
that code.

Added, by estimate: the component, the stack program, the two dynamic
resources and the two hooks, most of whose bodies are moved rather than
written; the driver, with the checks of §3.3 and §3.4; the new
`credentials` commands and the chain.

**The interactions that disappear**: a survey by name on every run; two
comparisons and two exit semantics in one report; three write phases,
each with its own failure analysis; the dump key's mint after the
terminate and its predecessor's retirement, ordered by hand; the
reserved address's re-point as an exception to a read-only run; and an
ordering across the terminate that a network change and a launch had to
agree on outside any engine.

**What it costs that the script did not**: a pull request per writing
run; the committed identifiers of §3.3; two hooks and a new tier of
test to hold them; the server and host keys as committed ciphertext;
and a migration with one adoption and one replacement.

--------------------------------------------------------------------------------

## 11. What is already conformant

*   **The box itself**: its Butane template, the dump script and its
    refusal of an archive that holds no stack, the dump's recipients,
    and the reboot window. They move (§8) and do not change.
*   **The standalone moves**, `dump` and `restore`, and the probe, which
    dials `settings.ADDRESS` with no credential.
*   **The escrow of the CA and the age generations**, and every playbook
    of physical/state-backend.md §7 but the lost state's.
*   **The layering contract, for the driver.** The driver is a script
    and imports no stack program or component; it runs the stack through
    the CLI and the Automation API, which start `kluster.main` as every
    run does. The edge the contract lacks is the other direction, from
    the declarations into `kluster.scripts`, which §8 adds.
*   **The dynamic-provider rules and the type tokens**, which the two new
    resources follow as written.

--------------------------------------------------------------------------------

## 12. What does not propagate

*   **From framework/ci.md §1, "no stack declares it".** What that
    protects is the bootstrap: the backend must exist before any stack
    whose state it holds. That still holds. The conclusion drawn from
    it, that no stack can declare the appliance, assumed that every
    stack keeps its state in the appliance, and stops holding here.
*   **From rule 6 of credentials.md §1, "State … never enters git."**
    That stays true of every stack whose state is in the appliance. A
    committed state has the configuration's exposure, and the rule's
    other half — that committed configuration carries only what a
    program needs before it runs — does not extend to the state: the
    state carries what the program generates, under the same passphrase
    the configuration is under (§3.3).
*   **From framework/pulumi.md §3.2, pins in `Pulumi.yaml`.** It does
    not reach a pin a script reads too (§8).
*   **From physical/state-backend.md §1, the host key "escrowed nowhere"
    and dying with its box.** The stable key reverses it, for the reason
    §4.4 gives.
*   **From the posted design: the state bucket and its key**, the chain
    entry for that key, and the rule that a run's backend and
    passphrase never reach it through `mise`, which survives as §9.2's
    rule for every operator stack. Ruling 1 kept the state in this
    repository, so neither the bucket nor its spike is built.
*   **From the posted design: importing the instance at the cutover.**
    An import runs no program, so the running box's `metadata` would be
    recorded in the clear (§3.3); the cutover replaces the box instead
    (§14, slice 6).
*   **From the posted design: a `mise run state-backend` task** beside
    `github`'s, replaced by §9's console script.
*   **From Pulumi's DIY backend, its lock as the lock between
    workstations.** The lock file lives in an ignored directory beside
    the checkpoint, which no other workstation sees; what stands between
    two workstations is the rebase that carries a checkpoint and the
    conflict it raises (§3.4).
*   **From rule 6 of credentials.md §1, "Deliberately not the desktop
    secret store".** It does not reach the operator passphrase, which
    the driver reads rather than a `mise` template (§6). It still holds
    for the stack passphrase and the client bundle.
*   **From rule 6 of credentials.md §1, "every credential a stack
    authenticates with is a config secret in that stack".** It does not
    reach the `operator` client bundle the hooks connect with, which
    authenticates to the estate's backend rather than to a provider of
    the stack, and which the stack program passes down from its slot
    (§4.3).
*   **From framework/dispatch.md §2, rule 5, "The primary workspace
    holds no work" and "restored with `jj new main` after each fetch".**
    The primary workspace's `@` may hold the operator's checkpoint, and
    it is rebased instead, whatever it holds (§3.4).
*   **From the script, the restore owed as a workstation slot.** The
    backend that answers with no stack is the record, and every
    workstation reads it (§3.4).
*   **From the weekly drift run of framework/ci.md §3.** It carries the
    stacks CI deploys and no operator stack; drift in one is read with
    `plan` (§9.2), and the appliance's liveness and certificate by the
    probe, as now.

--------------------------------------------------------------------------------

## 13. The documents this content lands in

| Document | What lands there |
| --- | --- |
| [framework/pulumi.md](../framework/pulumi.md) | framework/pulumi.md §3 gains the operator stacks: their census, the one driver and what it sets, from this document's §9; the committed checkpoint's layout, the checks a run makes around it and how it is landed, from §3.2, §3.3 and §3.4; and the new rules stated in §3.4, §4.3 and §9.2. framework/pulumi.md §3.2 gains the shared pin's exception stated in §8. |
| [framework/dispatch.md](../framework/dispatch.md) | Rule 5 of framework/dispatch.md §2: its headline, "The primary workspace holds no work", and its "restored with `jj new main` after each fetch" are both rewritten: the primary workspace's `@` holds what the operator left in it and is rebased after a fetch, never replaced, as this document's §3.4 states it. |
| [style/pulumi.md](../style/pulumi.md) | Under "Layering", the rules stated in §8, on `kluster.lib.<area>` and on the forbidden edge into `kluster.scripts`, and the bundle passed down as a parameter, stated in §4.3; under "Resources and their contents", the rules stated in §4.1 and §4.2. |
| [framework/testing.md](../framework/testing.md) | The engine test stated in §4.3, as a tier beside the Pulumi mocks. |
| [credentials.md](../credentials.md) | Rule 6 of credentials.md §1: the secret store as a channel for the operator passphrase, which rewrites the rule's "Deliberately not the desktop secret store" for that value alone, from this document's §6; the `operator` bundle's exception, from §4.3; and a committed state's exposure, from §12. credentials.md §3: the rows named in §6. credentials.md §4: the commands named in §5. credentials.md §4.4: the `operator.passphrase` slot, and the restore-owed record gone from the bundle's slot. |
| [physical/state-backend.md](../physical/state-backend.md) | physical/state-backend.md §1: the stack, the stable keys and the bill of materials of this document's §4.4. physical/state-backend.md §3: where the server key lives. physical/state-backend.md §4: the security list. physical/state-backend.md §5: the retention converged in place. physical/state-backend.md §7: the replacement's hooks, the restore owed read from the backend, and the lost state of this document's §3.6. And the operating form moved from `deploy/state-backend/README.md`, as §8 says. |
| [framework/ci.md](../framework/ci.md) | framework/ci.md §1: the appliance is a stack whose state is outside the backend it creates, as this document's §12 says, and its files are under `src/`. framework/ci.md §3: `checkpoints/` among the paths no preview reads. |
| [framework/github.md](../framework/github.md) | framework/github.md §1 and §3.1: the driver's command in place of the task. |
| [declarative/README.md](../declarative/README.md) | declarative/README.md §1: the census of stacks gains `state-backend`, the second operator stack. |
| [operations.md](../operations.md), [cluster/storage.md](../cluster/storage.md), [declarative/physical.md](../declarative/physical.md), [cluster/security-audit.md](../cluster/security-audit.md) | Where each names `state-backend provision` or a file of the directory. |
| AGENTS.md | The list of console scripts gains the driver. |
| `README.md` | The layout's `deploy/` row and a `checkpoints/` row; the workstation section's commands. |
| `escrow/README.md` | The renamed label. |

--------------------------------------------------------------------------------

## 14. How we get there

Order: slice 0 at any point before slice 5; slices 1 to 7 one after the
other, because each owns a file the next one does too, and the driver
comes before the passphrase's rename so that the rename never touches
`mise.toml`. Every test below runs with the providers stubbed — the
Pulumi mocks, the B2 fake and the existing OCI fakes — except the engine
test of slice 5, which runs the engine and no provider.

What else sequences them:

*   [Aetf/kluster#318](https://github.com/Aetf/kluster/pull/318) moves
    the Pulumi CLI's pin, which §3.2 measured at and §4.3's engine facts
    rest on: slice 0 measures at both versions.
*   [Aetf/kluster#298](https://github.com/Aetf/kluster/pull/298) and
    [Aetf/kluster#299](https://github.com/Aetf/kluster/pull/299)
    regenerate the B2 SDK, whose `Bucket` and `ApplicationKey` slice 5
    declares, so slice 5 follows them.
*   [Aetf/kluster#398](https://github.com/Aetf/kluster/pull/398) touches
    `pyproject.toml`, `renovate.json5`, `docs/operations.md` and
    `tests/test_conventions.py`, each of which a slice below owns; those
    slices follow it.
*   [Aetf/kluster#399](https://github.com/Aetf/kluster/pull/399) adds
    its row to the RFC index beside this document's, and whichever
    merges second rebases.
*   **Serialized files**: slices 1 and 2 touch `pyproject.toml`, slice 2
    touches AGENTS.md, and slices 1, 4 and 6 touch framework/ci.md, one
    after the other.

**Slice 0: the engine's facts, and the file backend's, at the pin. No
repository change.**

*   **Done means**: a transcript on the decision issue of the engine
    facts §4.3 lists, and of §3.2's facts, at the pin and at the version
    the open pin bump moves it to.

**Slice 1: `deploy/state-backend/` absorbed into `kluster.lib`,
behavior unchanged.**

*   **Done means**: the files of §8 are in
    `src/kluster/lib/state_backend/machine/` and `deploy/state-backend/`
    is gone; the render, the dump and restore and the readiness wait
    are in `kluster.lib.state_backend`, the render taking the keys and
    recipients as arguments and minting and recovering nothing, the
    script passing it what it mints and recovers today; what they
    import from the `credentials` package is in `kluster.lib` or stays
    with the script, as §8 lists; the forbidden contract of §8 is in
    `pyproject.toml`; the template renders through
    `kluster.lib.templates` and `DEPLOY_DIR` is gone; `settings.py` is
    `kluster.lib.state_backend.settings`; the new rules of §8 are in
    style/pulumi.md and framework/pulumi.md §3.2; the README's content
    is in physical/state-backend.md; every document and comment that
    names a moved file names its new path; and every existing test
    passes with its claim unchanged.
*   **Owned paths**: `deploy/state-backend/`;
    `src/kluster/lib/state_backend/` (new), and the new modules beside
    `src/kluster/lib/workstation.py` that §8 moves out of the
    `credentials` package; `src/kluster/scripts/state_backend/`;
    `src/kluster/scripts/credentials/`'s `age`, `lifecycle`,
    `pulumi_config`, `b2` (a comment naming the dump script) and `cli`
    (the drill recipient's writer) modules; `pyproject.toml`
    (serialized: the contract); `renovate.json5`;
    `.github/workflows/noop-automerge.yml`, its comment; `README.md`;
    `docs/physical/state-backend.md`; `docs/framework/ci.md`
    (serialized); `docs/framework/pulumi.md`; `docs/style/pulumi.md`;
    `docs/operations.md`; `docs/credentials.md`, where it names the
    drill recipient's path; `docs/cluster/security-audit.md`, where it
    names the Butane template's; and the tests that read those files —
    `test_machine`, `test_state_dump`, `test_state_roles`,
    `test_provision`, `test_cli`, `test_drill_identity`,
    `test_workstation`, `test_noop_automerge`, and the helpers
    `tests/state_dump_box.py` and `tests/drill_recipient_redirect.py`.
*   **Tests that fail without it**: the render from an installed copy of
    the package, with no checkout around it; and `lint-imports` refusing
    a `kluster.lib` module that imports `kluster.scripts`.

**Slice 2: one driver for the operator stacks, with `github` its first
stack.** After slice 1.

*   **Done means**: the console script of §9.4 with `plan`, `up` and the
    passthrough; the operator-stack census in `conventions`; the
    stack-to-environment mapping in `kluster.lib`, which the
    `credentials` commands use too; the refusals of §9.2; the committed
    state's checks of §3.3 and §3.4, built and tested though no stack
    uses them yet; `mise.toml`'s `github` task and the template that
    exports that stack's passphrase removed, the driver finding it where
    the `credentials` commands already do; the workflow census reading
    the operator-stack census; the new rule on dispatch.md §2's rule 5;
    AGENTS.md's list of console scripts; and the docs of §13 for the
    driver.
*   **Owned paths**: `src/kluster/scripts/operator_stack/` (new);
    `src/kluster/lib/` (a new module beside `workstation`);
    `src/kluster/conventions/identity.py`;
    `src/kluster/scripts/credentials/pulumi_config.py` and `escrow.py`;
    `pyproject.toml` (serialized: the console script); AGENTS.md
    (serialized: the list of console scripts); `mise.toml`; the header
    comment of `Pulumi.github.yaml`, written so that it names the
    passphrase by what it is and slice 3's rename leaves it alone;
    `README.md`; `docs/framework/dispatch.md`;
    `docs/framework/github.md`; `docs/framework/pulumi.md`;
    `docs/credentials.md`; `tests/test_operator_stack.py` (new);
    `test_mise_env`, `test_conventions` and `test_pulumi_config`.
*   **Tests that fail without it**:
    *   the environment handed to `pulumi` names the stack's backend and
        its passphrase, and never the estate passphrase or a backend
        from the test's own environment;
    *   an argument naming a stack is refused, and so is a run under
        `.claude/`;
    *   `plan` makes no `up` call, and exits 0 or 1;
    *   over a scratch repository: a run refused while the remote's
        `main` is fetched but not an ancestor of `@`, naming the rebase,
        and while it is not fetched at all, naming the fetch; a run
        refused while the checkpoint is conflicted; and, with both
        true, the ancestry refusal given, not the conflict's;
    *   over a scratch backend: an `up` over nothing planned, and a
        writing command over an unchanged deployment, each leave the
        file's bytes as they were; and an `up` over nothing planned
        runs while a failed check is recorded, and clears the record;
    *   a checkpoint carrying a secret value in the clear, and one
        carrying a declared-secret property in the clear, each fail the
        run naming the property;
    *   a passed-through `import` refused for a committed stack.
*   **Live**: `operator-stack github plan` from the workstation, which
    plans nothing, and an `up` with nothing to do, which writes
    nothing.

**Slice 3: the operator passphrase.** After slice 2.

*   **Done means**: the `github` stack passphrase is renamed throughout —
    the register row, the escrow label `operator/passphrase` with its
    ciphertext moved, the slot `operator.passphrase`, the variable
    `KLUSTER_OPERATOR_PASSPHRASE`, the commands
    `credentials derived operator-passphrase generate|recover`; the
    driver and the `credentials` commands resolve it through §6's
    chain; rule 6 of credentials.md §1 has the new channel and the
    reversal §6 names; the pull request names the one step each
    existing workstation takes; and the sweep for claims the change
    made false runs by concept, a passphrase held apart, and not by
    identifier.
*   **Owned paths**: `src/kluster/scripts/credentials/`'s
    `workstation`, `slots`, `escrow`, `pulumi_config`, `cli`, `masters`
    and `kdbx` modules; the driver's passphrase lookup in `kluster.lib`;
    `escrow/github/passphrase`, moved to `escrow/operator/passphrase`;
    `escrow/README.md`; `README.md`; `docs/credentials.md`;
    `docs/framework/github.md`; and the tests `test_slots`,
    `test_secret_reprs`, `test_devices`, `test_cli_help`,
    `test_conventions`, `test_masters`, `test_escrow`,
    `test_pulumi_config`, `test_operator_stack` and
    `test_root_credentials`, and `tests/root_credentials.py`.
*   **Tests that fail without it**: the chain's order, over a fake
    keyring; a refusal on a machine without the passphrase that names
    the new command; and the help and refusal texts in the new term.

**Slice 4: the stack's configuration, and its committed checkpoint.**
After slice 3.

*   **Done means**: `state-backend` is in the stack names and in the
    operator-stack census as a committed stack, with a stack program
    that raises from its entrypoint, as AGENTS.md has an unwritten stack
    do; `.gitignore` names what §3.4 keeps out of `checkpoints/`; the
    path is inert to the pull-request previews, in `preview.yml`'s filter
    and in framework/ci.md §3's list, since no stack CI runs reads it; a
    test holds §3.3's second check over every committed checkpoint; the
    new commands fill the configuration — `b2-state-backend-management
    mint`, `state-backend-server issue` (the key and certificate, and
    the CA's certificate in the clear) and `state-backend-host-key
    generate`, which also writes the committed public half;
    `backup-age-<N> generate` writes the recipients file, once for the
    generations already escrowed, and `credentials derived check` holds
    that file to the escrow; and the register rows of §6 are in, but
    the OCI key's, which waits for slice 6.
*   **Owned paths**: `src/kluster/scripts/credentials/`'s `b2`,
    `derived`, `cli`, `entries`, `slots`, `pki` and `age` modules;
    `src/kluster/conventions/identity.py`;
    `src/kluster/stacks/__init__.py` and
    `src/kluster/stacks/state_backend.py` (new, raising); `.gitignore`;
    `.github/workflows/preview.yml`; `docs/framework/ci.md`
    (serialized); `src/kluster/lib/state_backend/machine/`, the two new
    files; `docs/credentials.md`; and the tests `test_b2`,
    `test_derived`, `test_slots`, `test_age`, `test_pki`,
    `test_conventions` and `tests/test_checkpoints.py` (new).
*   **Tests that fail without it**: `issue` writes a key and a
    certificate that chain to the CA and name `settings.ADDRESS`; the
    committed public half matches the configured host key; `check`
    refuses a recipients file that differs from the escrow; the
    census's every stack has a program and the new one raises from it;
    and a committed checkpoint with a declared-secret property in the
    clear fails the checkpoint test.
*   **Live, by the operator, after the merge**: `operator-stack
    state-backend pulumi stack init`, whose first checkpoint the
    operator lands as §3.4 says, then the mints, whose configuration
    lands as `Pulumi.state-backend.yaml` the way every command that
    writes a stack's configuration lands today. The slice's own pull
    request carries neither file: only the operator's workstation holds
    the passphrase they are written under.

**Slice 5: the stack, declared and not applied.** After slice 4 and the
B2 SDK's regeneration.

*   **Done means**: the component of §4.1, its stack program in place of
    the raising one, the two dynamic resources in `kluster.providers`
    with their type tokens, the hooks and the bundle they are handed,
    the engine test, and the new rules of §4.1, §4.2 and §4.3 in their
    documents. Nothing is applied.
*   **Owned paths**: `src/kluster/components/state_backend/` (new);
    `src/kluster/stacks/state_backend.py`; two new packages under
    `src/kluster/providers/`; `tests/test_state_backend_stack.py` and
    `tests/test_state_backend_engine.py` (new); `docs/style/pulumi.md`;
    `docs/framework/pulumi.md`; `docs/framework/testing.md`;
    `docs/credentials.md`, for the bundle's exception.
*   **Tests that fail without it**:
    *   under mocks: the list holds the declared rules and nothing else,
        a case per mutated rule; the subnet carries only the program's
        list and table; the VCN, the subnet, the address and both
        buckets are protected; the instance carries the options of §4.2
        and `metadata` is secret, and a mutation dropping
        `ignore_changes` goes red; the readiness resource replaces on
        the instance's id; `createdBy`, `applicationKeyId` and the
        provider's user and fingerprint are secret; two renders from
        the same inputs are equal byte for byte, and each single-input
        change moves exactly its own digest; the dump key's
        capabilities, bucket and prefix are the declared ones; and the
        hooks are bound where §4.3 says;
    *   the hooks as functions: without the permission, the create and
        delete hooks refuse and nothing is dumped; with it, the delete
        hook dumps and keeps the plaintext; a failed dump raises; the
        restore hook restores from the plaintext; and without one, over
        a backend that holds no stack, it refuses naming
        `state-backend restore`;
    *   the dynamic resources: the upload refuses a truncated artifact
        and one with the wrong digest; the wait refuses a certificate
        naming the wrong address;
    *   the engine test's facts (§4.3).

**Slice 6: the cutover.** After slice 5.

*   **Done means**: the `state-backend` gate of §7 in the driver, the
    address check with it, and `plan`'s exit 3 read from the backend;
    the OCI key's mint writing configuration rather than its slot; a
    one-time `state-backend adopt` that writes the ids of every existing
    resource the stack keeps — all but the instance and the script's
    dump key — into the stack's configuration, which the component
    passes to each resource as its `import_` option; the old path
    deleted — `provision.py` but `ssh` and its pin checks, the provision
    orchestration in `cli.py`, the restore-owed slot, from `config.py`
    the escrow's roots, the renewal clock and the metadata readers, from
    `b2.py` the bucket's and the dump key's code; every command and
    message that names `state-backend provision` naming its successor;
    and the documents of §13 that describe the run.
*   **Owned paths**: `src/kluster/scripts/operator_stack/`;
    `src/kluster/scripts/state_backend/`;
    `src/kluster/scripts/credentials/`'s `b2`, `oci_slot`, `derived`,
    `cli`, `slots`, `lifecycle` and `oci_iam` modules;
    `src/kluster/components/state_backend/`;
    `src/kluster/lib/state_backend/`; `tests/test_provision.py`,
    `tests/test_b2.py`, `tests/test_operator_stack.py`,
    `tests/test_drill_credentials.py`; `docs/physical/state-backend.md`;
    `docs/framework/ci.md` (serialized); `docs/declarative/README.md`;
    `docs/credentials.md`; `docs/operations.md`;
    `docs/cluster/storage.md`; `docs/declarative/physical.md`;
    `README.md`.
*   **Tests that fail without it**, over a faked Automation API
    workspace: with a replacement pending and no `--force`, no `up`
    call, and the digests that moved named; `--force` sets the
    permission, and `--replace` passes the instance's URN; a create of
    the instance while the refreshed address is assigned and the state
    holds no instance is refused with `--force`; an expiry inside the
    margin is named; `plan` exits 3 over a backend that holds no stack,
    and 4 over one that does not answer.
*   **Live drill**, the falsifier for import fidelity, refresh drift and
    the hooks against OCI. The instance is not imported (§3.3), and the
    checks of §3.3 run after every writing step:
    1.  `adopt`, then `plan`, with the old box still serving. It shows
        the imports, the migration — the list and the table created, the
        subnet moved to them — the new dump key, and a create of the
        instance, which the address check would refuse while the old box
        holds the address. Any other update on an imported resource is
        a finding the builder names and stops on, rather than an
        `ignore_changes` to add.
    2.  `state-backend dump`, into a file.
    3.  The old instance terminated by hand; the address is left
        pointing at nothing.
    4.  `up --force`: the imports, the migration, the launch, the
        address re-pointed, the wait. The readiness hook finds no dump
        of its own and a backend that holds no stack, names
        `state-backend restore`, and the run exits 3.
    5.  `state-backend restore <file>` of step 2's dump. `pulumi stack
        ls` on the estate's backend lists every stack it held; `plan`
        plans nothing and exits 0; and the operator lands the
        checkpoint.
    6.  By hand: an ingress rule added to the list, the retention set to
        29 days. `plan` names both, and `up` repairs them with the
        instance's OCID unchanged.
    7.  A bare `up --replace <instance URN>` passed through to `pulumi`
        fails at the hook, with the box untouched.
    8.  The old group, the dump key the script minted, and the OCI key's
        slot are deleted by hand; the pull request names each step.

**Slice 7: the adoption removed.** After slice 6.

*   **Done means**: `state-backend adopt`, the import ids in the stack's
    configuration and the component's `import_` options are gone, and
    `plan` then plans nothing; the lost-state playbook in
    physical/state-backend.md §7 names the same adoption for a lost
    state — every resource but the instance through the program, the
    instance replaced (§3.6) — with the ids read from the console.
*   **Owned paths**: `src/kluster/scripts/state_backend/`;
    `src/kluster/components/state_backend/`; the adoption keys in
    `Pulumi.state-backend.yaml`, which are plaintext;
    `docs/physical/state-backend.md`; `tests/test_state_backend_stack.py`
    and `tests/test_provision.py`.
*   **Tests that fail without it**: none is new; the component's case
    that passes `import_` ids goes, and the stack's cases pass without
    it.
*   **Live**: `plan` plans nothing after the merge.

--------------------------------------------------------------------------------

## 15. Open questions

### 15.1 Rulings

Every question below was answered at its recommended option, (a), on
2026-09-28; the text of §3 to §14 is written to those answers. The
approach — the whole appliance as one stack — is ruled, and so is
how a committed checkpoint is landed: as an ordinary file changed in
`@`, described and pushed by the operator like any other change, with
the primary workspace's `@` rebased onto `main` after every fetch. None
of that is asked again.

1.  **Where the state lives.**
    *   **(a) Recommended**: committed to this repository, with the
        checks of §3.3 and §3.4.
    *   (b) B2, through Pulumi's S3 backend, in a bucket of its own,
        gated on slice 0's B2 half; Pulumi Cloud's free tier if that
        fails.
    *   (c) Committed, with the whole file encrypted (§3.3).
2.  **The driver.**
    *   **(a) Recommended**: one console script, `operator-stack`, named
        after the passphrase and the census, replacing `mise run github`
        and `state-backend provision`.
    *   (b) The same script, with a `mise run <stack>` task per operator
        stack kept as a shorthand for it.
    *   (c) Two drivers sharing only the environment mapping.
3.  **What `up` does when the box's replacement is pending.**
    *   **(a) Recommended**: it writes nothing, names the replacement and
        exits 1. A repair in place waits for the box only when a
        replacement is pending beside it; alone, it is applied.
    *   (b) It applies the in-place changes with the box held, through a
        switch that adds `ignore_changes` on every input that would
        replace the box, then exits 1. That is a second declaration of
        the instance to test.
4.  **The restore.**
    *   **(a) Recommended**: inside the run, in the readiness resource's
        `after_create` hook. The kit is needed only for a box the run
        did not dump for itself.
    *   (b) The same, plus a handover age identity in the stack's
        configuration, so a restore from another workstation needs no
        kit either: one more stable key.
    *   (c) Outside the run: the driver exits 3 after every replacement,
        as `provision` does today.
5.  **How the server key and certificate are minted.**
    *   **(a) Recommended**: by a command into configuration, with the CA
        kept in escrow.
    *   (b) The CA's key goes into the stack's configuration, and
        `pulumi_tls` issues the certificate in the program, with early
        renewal as a planned replacement. That adds a dependency, an edit
        to `pyproject.toml`.
6.  **The passphrase.**
    *   **(a) Recommended**: the operator passphrase, covering the
        operator stacks, found through the roots' chain, with the new
        channel in rule 6 of credentials.md §1.
    *   (b) The same, named the workstation passphrase.
    *   (c) The same name, from the slot alone: no chain and no new
        channel.
7.  **The Fedora CoreOS image.**
    *   **(a) Recommended**: a pinned release. A bump imports a new image
        and does not replace the box, whose running OS Zincati keeps
        current.
    *   (b) The stream followed on every run, which imports each release
        as it ships.
8.  **The retention converges in place in both directions.**
    *   **(a) Recommended: yes.**
    *   (b) A longer rule waits for `--force`.
9.  **Where the appliance's pins live.**
    *   **(a) Recommended**: `kluster.lib.state_backend.settings`, with the
        exception that §8 writes into framework/pulumi.md.
    *   (b) `Pulumi.yaml`'s `versions:` block, with a reader of that file
        that a script can use outside a program.
10. **Where the dump a replacement takes is kept.**
    *   **(a) Recommended**: on the workstation that took it, encrypted,
        as today. A restore from another workstation copies it, or takes
        the newest nightly dump (§3.4).
    *   (b) Uploaded by the delete hook to the dump bucket too. That
        hands the hook a B2 key that can write there, a credential read
        outside the line that builds a provider, which style/pulumi.md's
        "Layering" forbids; it would need a rule of its own.

### 15.2 Settled on first contact

*   **Import fidelity**: whether a resource imported through the program
    plans anything beyond the migration. Slice 6's first step is its
    falsifier.
*   **Whether the tenancy's default tags exist.** The first checkpoint
    shows it; §3.3's checks do not depend on it.
*   **§3.2 at a new pin**: slice 0 measures it again, and every later
    pin bump's review includes a `plan` from a workstation, since CI
    runs no operator stack.
