# The state-backend appliance

The Postgres instance holding this project's Pulumi state, on an OCI
`VM.Standard.E2.1.Micro`. It is a **bootstrap dependency**: Pulumi cannot
create it, because Pulumi needs it to do anything at all. Design and rationale
live in [docs/physical/state-backend.md](../../docs/physical/state-backend.md);
this file is how to operate it.

| Path | What |
| --- | --- |
| `butane.yaml.j2` | The machine, whole: Postgres quadlet, PKI, `pg_hba`, age recipients, the dump timer, the reboot window. |
| `state-dump.py` | What that timer runs — `pg_dump` → `pg_restore --list` → age → B2, standard library only. |
| `operator-keys.txt` | SSH keys for diagnosis (`state-backend ssh`). The box is never configured by hand, and a key absent here means no access until the next re-provision. |

The code that renders and applies these is `src/kluster/scripts/state_backend/`,
exposed as the `state-backend` console script.

## Provisioning

Runs on the workstation holding the offline kit. The CA and the backup
encryption identities come out of the escrow registry — the first run generates
them and commits their ciphertexts, every run after opens the same ones with
the kit's recovery key (docs/credentials.md §2.2) — and the B2 credentials are
minted from the seed key:

```sh
mise x uv -- uv run state-backend provision
```

The kit is `.credentials/kit.kdbx` in the checkout unless `$KLUSTER_KDBX`
names one elsewhere — on removable media, or shared between checkouts.

Idempotent end to end, so this is equally the bring-up command and the
re-provision command; that is what keeps the rebuild path warm. It creates (or
converges) the appliance's own VCN, subnet, gateway, security group and
reserved public IP, imports the pinned Fedora CoreOS release as a custom image,
renders the Ignition and launches the instance — then writes the operator's
client bundle to `.credentials/state-backend/` in the checkout, the workstation
slot for it (docs/credentials.md §4.4).

**It applies the current commit.** A run compares the box to the repository —
the Butane file, the operator keys, the pins, the certificate identities, the
B2 dump key's scope — and one thing to the clock: how much life the box's
server certificate has left, which is drift once it is inside the renewal
margin — so a coming expiry is something a run reports rather than something
anyone has to watch a calendar for. Acting on it is still `--force`, like any
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
| `0` | The appliance is current and holds its state. | Nothing. |
| `3` | The box was replaced; the state is not back in it. | Run the `state-backend restore` the run printed. |
| `1` | The run failed. | Read the error, then the run's last words: they name a dump to restore if it had already replaced the box, and are silent if the old box is still serving. |

`1` covers a run that stopped before touching anything **and** one that
stopped after destroying the box, so it cannot be read as "nothing happened";
only the run's own output distinguishes them. `3` is the one status that
always means the same thing — the appliance is up, its database is empty, and
the dump to feed it has been named — which is why the replacement does not
reuse `1`. Neither non-zero status says the state is safe.

Other commands:

```sh
state-backend render --address <ip>   # the Ignition, without touching the cloud
state-backend bundle ci --address <ip>  # the CI client certificate and its URL
state-backend pins                    # verify the pinned digests (CI runs this)
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
`ci` in the pipeline.

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
— and age-encrypted to the `backup/age/<generation>` identities —
random at creation, their only stored copies the ciphertexts under `escrow/`,
which the kit's recovery key opens — and lands in B2 under a prefix whose
lifecycle rule enforces retention. Recovery is
a re-provision followed by `pg_restore` of the newest object — the same path
the quarterly drill exercises, which is why nothing about it is improvised.
