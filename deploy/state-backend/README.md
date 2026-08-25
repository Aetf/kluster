# The state-backend appliance

The Postgres instance holding this project's Pulumi state, on an OCI
`VM.Standard.E2.1.Micro`. It is a **bootstrap dependency**: Pulumi cannot
create it, because Pulumi needs it to do anything at all. Design and rationale
live in [docs/physical/state-backend.md](../../docs/physical/state-backend.md);
this file is how to operate it.

| Path | What |
| --- | --- |
| `butane.yaml.j2` | The machine, whole: Postgres quadlet, PKI, `pg_hba`, age recipients, the dump timer, the reboot window. |
| `state-dump.py` | What that timer runs — `pg_dump` → age → B2, standard library only. |
| `operator-keys.txt` | SSH keys for diagnosis. The box is never configured by hand. |

The code that renders and applies these is `src/kluster/scripts/state_backend/`,
exposed as the `state-backend` console script.

## Provisioning

Runs on the workstation holding the offline database — the PKI and the
encryption identities are derived from the derivation seed, and the B2
credentials are minted from the seed key:

```sh
export KLUSTER_KDBX=~/path/to/kluster.kdbx
mise x uv -- uv run state-backend provision
```

Idempotent end to end, so this is equally the bring-up command and the
re-provision command; that is what keeps the rebuild path warm. It creates (or
converges) the appliance's own VCN, subnet, gateway, security group and
reserved public IP, imports the pinned Fedora CoreOS release as a custom image,
renders the Ignition and launches the instance — then writes the operator's
client bundle to `~/.config/kluster/state-backend/`.

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

```sh
export PULUMI_BACKEND_URL="postgres://operator@<ip>:5432/pulumi_state?sslmode=verify-full&sslrootcert=$HOME/.config/kluster/state-backend/ca.crt&sslcert=$HOME/.config/kluster/state-backend/client.crt&sslkey=$HOME/.config/kluster/state-backend/client.key"
export PULUMI_CONFIG_PASSPHRASE=...    # derived from the derivation seed
```

The certificate's Common Name *is* the Postgres role: `operator` locally,
`ci` in the pipeline.

## Changing it

**Re-provision is the only apply path.** Nothing on the box is mutated in
place: a change is a PR against `butane.yaml.j2` (or the pins in
`settings.py`), then a re-provision — minutes of downtime on 5432, which CI
retries through and local runs re-run. SSH exists for diagnosis only.

Because the OS and Postgres both follow their streams automatically, and
because the machine carries nothing that `pg_dump` plus a re-provision cannot
rebuild, "the repo describes the box" stays true without a configuration agent
to enforce it.

## Losing it

The daily dump is age-encrypted to identities derived from the derivation
seed and lands in B2 under a prefix whose lifecycle rule enforces retention.
Recovery is
a re-provision followed by `pg_restore` of the newest object — the same path
the quarterly drill exercises, which is why nothing about it is improvised.
