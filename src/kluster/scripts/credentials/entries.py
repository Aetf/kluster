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
    user's OCID, a GitHub App's client id, the recovery key's age recipient.
    A credential with no such half (the ZeroTier token) records what it is
    instead, so the field is never empty and never a secret.
-   **Password** is the secret itself, and nothing else is.
-   **Attachments** carry key material that is a file rather than a string
    (the GitHub App private keys, the OCI API key).
-   **Protected custom attributes** carry what is left over when a credential
    is more than an identifier and a secret: the OCI row's tenancy OCID, which
    cannot go in UserName because the user OCID is there, and the identity
    domain its keys are retired through, which is a property of the tenancy
    rather than of the key. Protected rather than plain because both are
    account identifiers, and the entry has no reason to hand one out in a
    listing.

Adding a seed means adding a row here and a row in §2, in the same change.
"""

from __future__ import annotations

from dataclasses import dataclass

GROUP = 'seeds'

#: The OCI row's parts that are not fields. Named here, with the row, and used
#: by the minter that writes them.
OCI_KEY_ATTACHMENT = 'api-key.pem'
OCI_TENANCY_ATTRIBUTE = 'Tenancy OCID'
#: Where the identity-domains API for this tenancy answers. Retiring an API
#: key goes through it (`oci_iam.py`), and its endpoint is a property of the
#: tenancy rather than of the region, so it cannot be recovered from a
#: constant the way the region is.
OCI_DOMAIN_ATTRIBUTE = 'Identity domain URL'


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

    #: Protected custom attributes the row carries beyond UserName and
    #: Password. Enumerated so a kit can be checked against the register
    #: rather than against someone's memory of it.
    attributes: tuple[str, ...] = ()

    #: An action this row needs beyond `create` and `rotate`, as (name,
    #: help). A row that grew an attribute after kits were already in the
    #: field needs an explicit way to fill it in on an old one -- explicit
    #: because the repair borrows an account root, and routine rotation must
    #: not quietly start needing one.
    repair: tuple[str, str] | None = None

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
            member='recovery',
            title='Recovery key',
            identifier='the age recipient (its public half)',
            mints='nothing; it opens the escrow, which holds the generated secrets (§2.2)',
            self_reproducing=False,
        ),
        Seed(
            member='oci',
            title='OCI seed API key',
            identifier='the user OCID',
            mints='the per-stack OCI users and their API keys',
            self_reproducing=True,
            attachment=OCI_KEY_ATTACHMENT,
            attributes=(OCI_TENANCY_ATTRIBUTE, OCI_DOMAIN_ATTRIBUTE),
            repair=('domain', 'record the tenancy identity domain on a row that predates it'),
        ),
        Seed(
            member='cloudflare',
            title='Cloudflare seed token',
            identifier='the token id',
            mints='the zone-scoped provider token, the DNS-01 token, the gateway ACME token',
            self_reproducing=False,
            console=(
                'dash.cloudflare.com → My Profile → API Tokens → Create Token\n'
                '  → "Create Additional Tokens" template, named "kluster-seed".\n'
                '  Two permission rows, both of them: User → API Tokens → Edit,\n'
                '  which the template fills in, and Zone → Zone → Read, added by\n'
                '  hand, with Zone Resources left at all zones. The second is not\n'
                '  optional: a zone-scoped token names its zones by id, and the\n'
                '  ids are looked up through this seed, so a seed that cannot list\n'
                '  zones mints nothing.\n'
                '  Cloudflare refuses to let a token mint a token carrying token\n'
                '  permissions, so this one cannot be minted and cannot mint its\n'
                '  successor -- rotation is this same visit again, and the\n'
                '  superseded token is deleted on the same page.\n'
                '  A permission added to a token that already exists does not\n'
                '  extend the value already in hand, so a seed with the wrong\n'
                '  permissions is replaced rather than edited: make a new token\n'
                '  with both rows, record it here, delete the old one. An\n'
                '  account-root token held elsewhere is the seed only if it\n'
                '  already carries both.'
            ),
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
            identifier='a name for this token (Central shows it beside the value)',
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
