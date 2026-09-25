# The state-backend appliance

The Postgres instance holding this project's Pulumi state, on an OCI
`VM.Standard.E2.1.Micro`. It is a **bootstrap dependency**: Pulumi cannot
create it, because Pulumi needs it to do anything at all. Design and rationale
live in [docs/physical/state-backend.md](../../docs/physical/state-backend.md);
this file is how to operate it.

| Path | What |
| --- | --- |
| `butane.yaml.j2` (template) | The machine, whole: the Postgres unit — a plain systemd unit running `podman run`, auto-updated by label, not a quadlet — PKI, `pg_hba` and the Postgres roles, the age recipients, the unit that installs the pinned `age`, the dump timer, the reboot window. |
| `state-dump.sh` | What that timer runs — `pg_dump` → `pg_restore --list` → age → B2. Shell, because the box has no interpreter: it uses what the Fedora CoreOS image ships plus the `age` the template installs, and a test holds the template to that (docs/physical/state-backend.md §1). |
| `operator-keys.txt` | SSH keys for diagnosis (`state-backend ssh`). The box is never configured by hand, and a key absent here means no access until the next re-provision. |
| `drill-recipient.txt` | The public half of the drill age identity, one recipient. Written by `credentials derived drill-age-identity generate` — which pushes the private half into the ops repository's `drill` Environment first — and committed; absent until that generator has run, and the appliance then encrypts to the escrowed generations alone. |

The code that renders and applies these is `src/kluster/scripts/state_backend/`,
exposed as the `state-backend` console script.

## Provisioning

Runs on the workstation holding the offline kit. The CA and the backup
encryption identities come out of the escrow registry — the first run generates
them and commits their ciphertexts, every run after opens the same ones with
the kit's recovery key (docs/credentials.md §2.2) — the drill recipient is read
from the committed file above, and the B2 credentials are minted from the seed
key:

```sh
mise x uv -- uv run state-backend provision
```

The kit is `.credentials/kit.kdbx` in the checkout unless `$KLUSTER_KDBX`
names one elsewhere — on removable media, or shared between checkouts.

Idempotent end to end, so this is equally the bring-up command and the
re-provision command; that is what keeps the rebuild path warm. A run that
launches a box — one that finds none, or a replacement asked for below — creates (or
converges) the dump bucket and the appliance's own VCN, subnet, gateway,
security group and reserved public IP, and imports the current release of the
Fedora CoreOS stream `settings.py` pins, all while any old box still serves;
then it renders the Ignition and launches the instance. A run that leaves the
box standing creates nothing; the one write it can make is pointing the
reserved address back at a box that matches, when the address points anywhere
else. A run that
leaves a box to reach — one it matched, or one it just launched — writes the
operator's client bundle to
`.credentials/state-backend/` in the checkout, the workstation slot for it
(docs/credentials.md §4.4).

**It applies the current commit.** A run compares the box to the repository —
the Butane file, the operator keys, the age recipients, the pins, the
certificate identities, the B2 dump key's scope — and one thing to the clock:
how much life the box's server certificate has left, which is drift once it is
inside the renewal margin — so a coming expiry is something a run reports
rather than something anyone has to watch a calendar for. Acting on it is still `--force`, like any
other replacement. A matching box is left untouched, including its dump key,
whose secret exists only in the Ignition it booted with. `--replace` forces the rebuild when there is no diff
to find (rotating the dump key or the server key, or discarding a box that is
broken in a way its metadata cannot show).

**Finding drift is not permission to act on it.** The box holds every stack's
state and its boot volume goes with it, so a run that finds drift says what it
would replace and stops:

```sh
state-backend provision --force   # replace the box that is running
```

There is no prompt behind that flag: the replacement decision reads nothing
from a terminal, so it means the same thing in a script as it does by hand.
(The run as a whole still asks for the kit password when the desktop secret
store does not hold one — that is the one place a `provision` waits.)

Once asked for, the run dumps the running box and verifies the dump before it
terminates anything, printing the path it wrote — that file is what
`state-backend restore` takes afterward, and `--dump-output` puts it
somewhere other than the working directory. The replacement therefore depends
on the dump: a dump that fails stops the run with the box still standing, and
`--no-dump` is how an operator says the box cannot be dumped at all (it is
unreachable, or Postgres will not start) and accepts losing everything since
the last nightly dump.

**A run that replaced the box exits non-zero, and says why.** What it leaves
behind is an appliance answering on 5432 over an empty database, so its last
words are the dump's path and the `state-backend restore` that puts the state
back. Reaching zero takes that second command.

The three statuses are an interface, so a script can branch on them
(`provision --help` says the same):

| Exit | What happened | What to do |
| --- | --- | --- |
| `0` | The appliance is current, and no replacement run from this checkout left a restore owed. | Nothing. |
| `3` | The box was replaced — by this run, or by an earlier one whose restore is still owed, which is what a re-run after a replacement that stopped part way meets — and the state is not back in it. | Run the `state-backend restore` the run printed; a restore over the workstation slot's bundle is what brings the next run back to `0`. |
| `1` | The run failed. | Read the error, then the run's last words: once the run has started terminating the old box they name the dump to restore and say how far the replacement got, and they are silent when it stopped before that, with the old box still serving. |

`1` covers a run that stopped before touching anything **and** one that
stopped after destroying the box, so it cannot be read as "nothing happened";
only the run's own output distinguishes them. `3` is the one status whose
meaning does not depend on how far the run got — the appliance is up, the
restore a replacement left owed has not been run over this checkout's bundle,
and the dump to feed it has been named — which is why the replacement does not
reuse `1`. It reads the database as empty on this checkout's record
(`.credentials/state-backend/restore-owed`) rather than by asking it, so a
record left standing after the state went back some other way — a restore from
another checkout or over another bundle, or a terminate that never took the old
box away — gives `3` over a database that serves its state. The run's words
say which of the two to act on: run the restore they name, or delete the record
when the box already holds its state. Neither non-zero status says the state
is safe.

Other commands:

```sh
state-backend render --address <ip>   # the Ignition, without touching the cloud
state-backend bundle ci --address <ip>  # the CI client certificate and its URL
state-backend pins                    # verify the pinned digests (CI runs this)
state-backend dump                    # a dump of the live state, encrypted like the nightly one
state-backend restore <dump>          # feed a dump into a provisioned box
state-backend probe                   # the scheduled checks, run by the ops repository
state-backend ssh                     # a diagnostic login; the box is never configured by hand
```

## Connecting

The backend speaks TLS with **mandatory client certificates**, and clients pin
the server by literal IP (`sslmode=verify-full`) so the hot path never depends
on DNS — which is itself something this backend deploys.

Nothing has to be exported by hand. `mise.toml` reads the checkout's
`.credentials/` and sets all five variables a `pulumi` run needs, so a command
run through `mise` is already connected:

```sh
mise x -- pulumi stack ls
```

`PULUMI_BACKEND_URL` is the string in the bundle, and it names the appliance
and none of the files:

```sh
postgres://operator@<ip>:5432/pulumi_state?sslmode=verify-full
```

The three files travel beside it as `PGSSLROOTCERT`, `PGSSLCERT` and
`PGSSLKEY`, resolved from the same slot the URL came from. That is the one
channel both libpq and the driver behind Pulumi's Postgres backend read, and
it is why no path is expanded inside a connection string: paths in the string
would make the recorded copy true of one directory on one machine. A bundle is
usable wherever its files are, so moving a checkout invalidates nothing;
`state-backend bundle operator --address <ip> --directory <where>` writes one
somewhere else when that is wanted.

`PULUMI_CONFIG_PASSPHRASE` comes from `.credentials/pulumi.passphrase`, which
`credentials derived pulumi-passphrase recover` writes from the escrow using
the kit's recovery key. A checkout that has the bundle but not the passphrase
needs that one command and nothing else.

The certificate's Common Name *is* the Postgres role: `operator` locally,
`ci` in the pipeline. Neither is a superuser: both act as the role that owns
the state, which holds what Pulumi's Postgres backend needs and nothing more,
and the box admits no other role over TCP. The superuser answers only on the
container's local socket, which is how the box's own initialization and its
dump timer reach it (docs/physical/state-backend.md §2).

## Changing it

**Re-provision is the only apply path.** Nothing on the box is mutated in
place: a change is a PR against `butane.yaml.j2` (or the pins in
`settings.py`), then `state-backend provision --force` — minutes of downtime on
5432, which CI retries through and local runs re-run. SSH exists for
diagnosis only.

Rotating the server certificate is that same command and nothing else: an
expiry inside the renewal margin is drift, so the converge re-issues the
certificate under the same CA, and the dump it took on the way is what the
following `state-backend restore` feeds back.

Because the OS and Postgres both follow their streams automatically, and
because the machine carries nothing that `pg_dump` plus a re-provision cannot
rebuild, "the repo describes the box" stays true without a configuration agent
to enforce it.

## Losing it

The daily dump is listed with `pg_restore --list` on the box before it leaves
it — an archive whose table of contents names no table is a dump of a database
that has lost its state, which is what a replaced box holds until its restore
— and age-encrypted to the recipients `config.age_recipients` renders into
the Butane file, one kind of recipient per reader. The escrowed
`backup/age/<generation>` identities serve the operator: random at creation,
their only stored copies the ciphertexts under `escrow/`, which the kit's
recovery key opens, so `state-backend restore` on the workstation opens a dump
through the kit. The drill recipient on file in this directory serves the
quarterly rebuild drill: its private half lives in the ops repository's `drill`
Environment and nowhere on disk, and the drill's `state-backend restore
<object> --identity-file <key>` needs no kit. Which generations are recipients,
how each kind rotates and why the drill key needs no generational pair is
docs/physical/state-backend.md §5; the register rows for both keys and the
drill's own OCI and B2 credentials are docs/credentials.md §3. The dump lands in
B2 under a prefix whose lifecycle rule enforces retention. Recovery is a
re-provision followed by `state-backend restore` of the newest object — the
path the drill is designed to exercise (docs/physical/state-backend.md §7.3),
and the operator form of it, run by hand against a scratch box with the kit, is
§7.3.1 there. As of 2026-09-25 the drill workflow is not written
(`kluster-ops#57`). The operator form has run: the §7.3.1 rehearsal on
2026-09-18 opened a workstation dump with the kit (a dump encrypted to the same
escrowed generation as the appliance's), restored it into a scratch box with its
rows, and selected a stack against it (`kluster-ops#281`); and the first
production replace-and-restore followed on 2026-09-25 (`kluster-ops#385`). Two
halves of the path are unproven as of 2026-09-25, and so assumed broken rather
than known to work: opening, with the kit, an object the appliance uploaded
itself, and the drill key's `--identity-file` restore.
