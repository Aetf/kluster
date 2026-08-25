"""Where each seed lives in the kit, and the rule that puts it there.

This is §2's table in machine-readable form. It exists because `bootstrap`
has to *write* every row into a database it just created (§4): a naming rule
spread across each credential's own module cannot be enumerated, and what
cannot be enumerated cannot be built from nothing or checked against the
register.

The rule, in full:

-   **Path** is `seeds/<name>`, one group deep. The kit holds nothing but §2,
    so a deeper hierarchy would only encode distinctions the register does not
    make.
-   **UserName** is the credential's public identifier -- the half that
    appears in logs and consoles and is not a secret: a B2 key id, an OCI
    user's OCID, a GitHub App's client id. A credential with no such half
    (the ZeroTier token) records what it is instead, so the field is never
    empty and never a secret.
-   **Password** is the secret itself, and nothing else is.
-   **Attachments** carry key material that is a file rather than a string
    (the GitHub App private keys).

Adding a seed means adding a row here and a row in §2, in the same change.
"""

from __future__ import annotations

from dataclasses import dataclass

GROUP = 'seeds'


@dataclass(frozen=True)
class Seed:
    """One row of credentials.md §2."""

    #: The `credentials seed <member>` name.
    member: str
    #: Title within the group; the entry path is `seeds/<title>`.
    title: str
    #: What the entry's UserName holds.
    identifier: str
    #: What this seed mints, in the register's words.
    mints: str
    #: Whether it can mint its own successor, or a console must (§2).
    self_reproducing: bool

    #: What a human has to do in a console when no API can do it. Empty for
    #: everything the scripts mint themselves. Stated here rather than in a
    #: runbook so that `bootstrap` can read it out at the moment it stops.
    console: str = ''

    #: A file the platform hands over once, stored as an attachment (§2.1)
    #: rather than in the password field.
    attachment: str = ''

    @property
    def entry(self) -> str:
        return f'{GROUP}/{self.title}'

    @property
    def manual(self) -> bool:
        """Whether creating this one is a console step rather than a call."""
        return bool(self.console)


SEEDS: dict[str, Seed] = {
    seed.member: seed
    for seed in (
        Seed(
            member='derivation',
            title='Derivation seed',
            identifier='derivation-seed',
            mints='every locally-generated secret, by derivation (§2.2)',
            self_reproducing=False,
        ),
        Seed(
            member='oci',
            title='OCI seed API key',
            identifier='the user OCID',
            mints='the per-stack OCI users and their API keys',
            self_reproducing=True,
        ),
        Seed(
            member='cloudflare',
            title='Cloudflare seed token',
            identifier='the token id',
            mints='the zone-scoped provider token, the DNS-01 token, the gateway ACME token',
            self_reproducing=True,
        ),
        Seed(
            member='b2',
            title='B2 seed key',
            identifier='the application key id',
            mints='the management key and every prefix-scoped writer key',
            self_reproducing=True,
        ),
        Seed(
            member='github-dispatch',
            title='GitHub App (dispatch)',
            identifier='the client id (the JWT issuer)',
            mints='installation tokens for kluster-ops contents:write',
            self_reproducing=False,
            console=(
                'github.com/settings/apps → New GitHub App named "kluster dispatch".\n'
                '  Permissions: Repository → Contents: Read and write. No webhook.\n'
                '  Install it on kluster-ops only, then generate a private key.\n'
                '  The JWT issuer is the *client id*, not the numeric app id.'
            ),
            attachment='private-key.pem',
        ),
        Seed(
            member='github-trigger',
            title='GitHub App (trigger)',
            identifier='the client id (the JWT issuer)',
            mints='installation tokens for kluster actions:write',
            self_reproducing=False,
            console=(
                'github.com/settings/apps → New GitHub App named "kluster trigger".\n'
                '  Permissions: Repository → Actions: Read and write. No webhook.\n'
                '  Install it on kluster only, then generate a private key.\n'
                '  The JWT issuer is the *client id*, not the numeric app id.'
            ),
            attachment='private-key.pem',
        ),
        Seed(
            member='zerotier',
            title='ZeroTier Central API token',
            identifier='zerotier-central',
            mints='nothing; it is itself the provider credential',
            self_reproducing=False,
            console=(
                'my.zerotier.com → Account → API Access Tokens → New Token.\n'
                '  ZeroTier has no token API, so this is the one credential\n'
                '  that cannot mint its own successor.'
            ),
        ),
    )
}

#: The seeds a console must create, because their platform has no API for it
#: (§2). `bootstrap` and `rotate` stop and print the steps rather than
#: pretending they can be automated.
MANUAL = tuple(seed.member for seed in SEEDS.values() if seed.manual)
