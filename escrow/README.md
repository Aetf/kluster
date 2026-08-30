# The escrow

Age ciphertexts, one file per credential per generation. Everything here
is encrypted to the recipients in `RECIPIENTS`, whose private half is a
single key held offline and nowhere else. Publishing the ciphertexts is
the design, not an accident: what protects them is the key, and the key
is not in this repository.

    escrow/RECIPIENTS              the age recipients every file is written to
    escrow/<label>/<n>.age         generation n of the credential called <label>

The labels are listed in
[`docs/credentials.md`](../docs/credentials.md) and enumerated in code in
`src/kluster/scripts/credentials/escrow.py`. They cover the secrets no
provider mints — the Pulumi state passphrase, the state-backend CA key,
the identities the state-backend's database dumps are encrypted to, the
token the alert poller reads with, and the private key of each of the two
single-purpose GitHub Apps. Certificates issued under that CA
are not here: they are re-issued from it on demand, so keeping a copy
would store a secret whose loss costs nothing.

## What the shape buys

Most credentials here are **random at creation**, and the file below one
is written in the same act that mints it — so no command can hand out a
generated secret the escrow does not carry. The rest are created in a
provider console that publishes no API for creating one, and are filed
exactly as they arrive; the command that files one first compares against
what is already here, so recording a key twice adds no generation.

Rotating **one credential** adds a generation to its own directory, and
only the consumer of that credential is re-run. Rotating the **offline
key** re-encrypts every file here to the successor and changes no
plaintext at all, which is why replacing the key costs nothing in
production.

## Reading one back

Recovery needs this directory, the `age` tool, and the offline key —
that is the whole list, and deliberately so:

    age --decrypt --identity <key file> escrow/pulumi/passphrase/1.age

`credentials derived <row> recover` is the same thing with the key taken
out of the offline database instead of a file — a row being the label
with `-` for `/`, so `pulumi/passphrase` above is `pulumi-passphrase`
there. `credentials derived check` needs no key at all: it reports
missing labels, files that are not age files, and generations that skip
a number.
