"""The credentials this system does not produce (docs/credentials.md §3): made by a person, or carried, and delivered.

Some of §3's rows are neither minted from a seed nor generated and escrowed.
The credential is made by a person — in the console that checks it, the
appliance's own or the provider's where the provider publishes no API for
making one, or by drawing it where no console does — and this side of the
system only delivers it:

-   the **UniFi API key**, which the controller mints for a dedicated local
    admin and shows once;
-   the **AdGuard admin login**, which *is* the API credential — AdGuard Home
    has no scoped API at all (the security audit's M6), so the account both
    instances carry is what a rewrite call authenticates as;
-   the **ZeroTier Central API token**, which Central mints only in its own
    web console and scopes to the whole account;
-   the **GitHub admin token**, a personal access token created in the GitHub
    UI, which is what the `github` stack declares the forge with and what
    every `credentials` command that pushes a GitHub secret authenticates as;
-   the **BGP session password**, the one row here no console makes: it is a
    shared secret both ends of the gateway's routing session are configured
    with, either end accepts any string, and the operator draws it. The
    `physical` stack writes it into the routing daemon's configuration on the
    device (`components/gateway/routing.py`), and the cluster's half is the
    same value sealed to the cluster (`sealing.py`), which `k8s-base`
    declares (declarative/cluster-infra.md §2).

None of them is a seed: a seed is a credential that mints successors
(`entries.py`), and each of these mints nothing. Losing one costs a console
visit — or, for the session password, a fresh draw — and a re-run of its
`record` command, which is also the whole of its rotation.

**A row here is a provider credential like any other**, and lives where every
other provider credential of this installation lives: the committed
configuration of the stack that reads it (credentials.md §1 rule 6). What
separates these from the minted rows is only how the value is obtained — a
person creates it, so a rotation is a console visit and a re-run rather than
one command — and that difference is in the source, not in the slot.

A row here is therefore a console instruction plus a push, and the command's
shape follows: print the steps that create the credential, take the value
without echoing it, and deliver it into the committed configuration of the
stack that reads it, proven by reading it back like every other config secret
(`pulumi_config.Stack.fill`). The console steps live beside the row for the
reason §2's do (`entries.py`): a runbook is a second place for them to be
wrong.

**A value may be handed in rather than typed**, which is what makes a scripted
run possible. A secret is supplied as a *path* and never as an argument — an
argument would put the credential in the process table of a shared machine —
where `-` reads standard input. A plain field is supplied as the value itself,
being an address rather than a credential.

**A row whose value the cluster needs too is sealed in the same run**
(`Device.sealed`): `record` seals it to the cluster's certificate and writes
the ciphertext where the stack that declares it reads it, once a cluster
exists to seal to. Before then -- the session password is recorded before
`physical` first brings the cluster up -- the run says so and delivers the
configuration alone, and `seal` is the step that writes the sealed copy once
the cluster's controller runs, reading the value back out of the stack that
holds it rather than asking for it again.

Two rows are sealed and nothing else (`SEALED_RECORDS`): alertmanager's
webhook and the mail relay's DKIM key, whose only consumers are in the
cluster, so the seal is the whole of their delivery. The DKIM key is not made
here at all: it is carried from the legacy cluster, and its row refuses a key
whose public half is not the one the `dns` stack publishes.

**Which stack takes a row is not an argument.** The credential authenticates
against one thing, and the stack that talks to that thing is the only consumer
there is: `physical` drives the UDM's Network API and the overlay's Central
account and writes the routing daemon's configuration onto the device, `dns`
writes the split-horizon rewrites on the AdGuard pair (declarative/dns.md §3),
and `github` declares the forge.

§3's other pasted row — the UDM SSH key and the libvirt identity — has no
member here, because there are no console steps to print for it: nobody
creates either in a console. The installation's other automation installs them
(§3), and the act on this side is a paste with nothing to guide.
"""

from __future__ import annotations

import base64
import getpass
import hashlib
import logging
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from cryptography.exceptions import UnsupportedAlgorithm
from cryptography.hazmat.primitives import serialization

from ... import conventions
from . import derived, pulumi_config, sealing
from .kdbx import KdbxError

log = logging.getLogger(__name__)

#: Reading a non-secret answer from the operator. Injected for the reason
#: `masters.Prompt` is: a test needs no terminal, while secrets go through
#: `getpass` and never echo.
Prompt = Callable[[str], str]

#: The path that means standard input, so a value can be piped in from another
#: tool. A name rather than `/dev/stdin`, which is not a file everywhere.
STDIN = '-'

#: The stacks that read these credentials, named from the module that already
#: names them so a device and a mint cannot disagree about where `physical`
#: and `dns` are spelled.
PHYSICAL_STACK = derived.PHYSICAL_STACK
DNS_STACK = derived.ZONES_STACK
GITHUB_STACK = derived.GITHUB_STACK

#: The row whose value is read back as well as delivered (`borrow`). Named
#: because a caller indexes `DEVICES` by it, and a member spelled in two places
#: is a member that can drift into a `KeyError`.
GITHUB_ADMIN = 'github-admin'

#: The repository permissions the GitHub admin token is made with, and the
#: access each needs. credentials.md §3 defines the set -- every permission a
#: call made as the token needs, by the `github` stack or by a command that
#: pushes a GitHub secret -- and says what needs each member; this is the one
#: copy of the members, which the console steps render and a test holds to
#: that section's list. `Metadata`, read, is not here: GitHub grants it to
#: every fine-grained token.
GITHUB_ADMIN_PERMISSIONS: tuple[tuple[str, str], ...] = (
    ('Administration', 'read and write'),
    ('Contents', 'read and write'),
    ('Issues', 'read and write'),
    ('Variables', 'read and write'),
    ('Actions', 'read'),
    ('Secrets', 'read and write'),
    ('Environments', 'read and write'),
)

#: The row whose slot map entry is more than its config key (`slots.py`): the
#: stack carries the value on to the device, so the map names that channel
#: beside the key this table delivers into.
BGP = 'bgp'


@dataclass(frozen=True)
class Field:
    """One value of a device credential, and every way the command may get it."""

    #: The option stem, and the key this field is addressed by in `given`.
    name: str
    #: The key it is delivered under: for a device row, the Pulumi config key,
    #: which the slot map imports so the map and the push cannot name
    #: different keys; for a sealed row, the data key of its sealed value.
    key: str
    #: What to ask for, in the operator's words.
    describes: str
    #: A secret is encrypted into the committed file and never echoed; a plain
    #: field is an address that file may carry in the clear (§4).
    secret: bool = True
    #: A value of several lines, such as a PEM key, which a prompt would cut
    #: at the first: with no file named, it is read whole from standard input,
    #: which has to be a pipe, since a terminal would echo it.
    multiline: bool = False

    @property
    def flag(self) -> str:
        """The option that supplies this field instead of a prompt.

        A secret names a *file*, because that is the only way to hand one in
        without putting it in the process table of a shared machine.
        """
        return f'--{self.name}-file' if self.secret else f'--{self.name}'

    @property
    def dest(self) -> str:
        """Where `argparse` puts that option, derived rather than declared twice."""
        return self.flag.removeprefix('--').replace('-', '_')

    def read(self, prompt: Prompt, title: str) -> str:
        """Ask the operator for it, never echoing a secret."""
        if self.multiline:
            if sys.stdin.isatty():
                raise KdbxError(
                    f'{title}: {self.describes} is several lines, so it is piped in or named with {self.flag}, '
                    'never typed at a terminal that would echo it'
                )
            value = sys.stdin.read().strip()
        elif self.secret:
            value = getpass.getpass(f'{title} — {self.describes}: ').strip()
        else:
            value = prompt(f'{title} — {self.describes}: ').strip()
        if not value:
            raise KdbxError(f'{title}: {self.describes} is required')
        return value

    def resolve(self, given: str | None, *, prompt: Prompt, title: str) -> str:
        """This field's value: what the command line handed in, or what is typed.

        An empty answer is refused here, where the source is still known, so
        the error can name it: a file whose producer failed is empty rather
        than absent, and a credential delivered as an empty string fails much
        later, in a stack nobody is watching.
        """
        # A multiline value named `-` is standard input all the same, so it
        # goes through the one reader that refuses a terminal.
        if given is None or (self.multiline and given == STDIN):
            return self.read(prompt, title)
        if not self.secret:
            value = given.strip()
        elif given == STDIN:
            value = sys.stdin.read().strip()
        else:
            value = Path(given).expanduser().read_text().strip()
        if not value:
            raise KdbxError(f'{title}: {self.describes} came through empty, so there is nothing to deliver')
        return value


@dataclass(frozen=True)
class Device:
    """One §3 row whose credential is made by hand: in the console that checks it, or drawn by the operator."""

    #: The `credentials derived <member> record` row name, which is also the
    #: name this row carries in the slot map.
    member: str
    #: The §3 "Credential" cell, verbatim. The slot map quotes it and a test
    #: holds the two against the document.
    register: str
    #: What the value is, in the operator's words — the phrase every prompt and
    #: every log line names it by.
    title: str
    #: The stack whose committed configuration reads it.
    stack: str
    #: What that stack holds afterwards, for the line the push ends on.
    holds: str
    #: How the credential is created, printed at the moment it is asked for.
    console: str
    fields: tuple[Field, ...]
    #: The sealed value the cluster's copy becomes, where the cluster needs
    #: the value too; each field is sealed under the data key of the same
    #: name as its own `name`.
    sealed: conventions.sealed.SealedValue | None = None


DEVICES: dict[str, Device] = {
    device.member: device
    for device in (
        Device(
            member='unifi',
            register='UniFi API key',
            title='the UniFi API key',
            stack=PHYSICAL_STACK,
            holds='the Network API key',
            console=(
                'The UniFi console → Settings → Admins & Users → Add Admin, as a\n'
                '  *local* admin rather than a Ubiquiti SSO account: a key inherits\n'
                '  the reach of the account it belongs to, so the account exists for\n'
                '  this credential and nothing else.\n'
                '  The controller offers API-key creation to a Super Admin alone, so\n'
                '  the confinement is cut at the application layer rather than at the\n'
                '  role: give that admin Full Management on the Network application\n'
                '  and no access to any other application. The key carries exactly\n'
                '  that, and no smaller key exists to ask for.\n'
                '  Then open that account → Create API Key. The key is shown once,\n'
                '  and re-running this command with a fresh one is the whole of a\n'
                '  rotation — delete the superseded key on the same page.\n'
                f'  The controller answers over ZeroTier at https://{conventions.overlay.UDM},\n'
                '  which the stack derives from that same constant — the address is\n'
                '  not recorded here, so there is no second copy of it to disagree\n'
                '  (physical/gateway.md §2.3).'
            ),
            fields=(Field('api-key', 'unifiApiKey', 'the API key the console showed once'),),
        ),
        Device(
            member='adguard',
            register='AdGuard API credentials',
            title='the AdGuard admin login',
            stack=DNS_STACK,
            holds='the admin login both AdGuard instances answer to',
            console=(
                'There is nothing to mint: AdGuard Home has no scoped API, so its\n'
                '  admin account is the API credential, and both instances carry\n'
                '  the same one — a rewrite is written to alice and bob directly,\n'
                '  with a single login (declarative/dns.md §3).\n'
                "  That account lives in each instance's own `AdGuardHome.yaml`, state\n"
                '  the device keeps: the initial state the `physical` stack installs\n'
                '  names no account (credentials.md §3). Changing it is a change on\n'
                '  the instances; this command delivers whatever they now answer to.'
            ),
            fields=(
                Field('username', 'adguardUsername', 'the admin username'),
                Field('password', 'adguardPassword', 'the admin password'),
            ),
        ),
        Device(
            member='zerotier',
            register='ZeroTier Central API token',
            title='the ZeroTier Central API token',
            stack=PHYSICAL_STACK,
            holds='the Central API token',
            console=(
                'my.zerotier.com → Account → API Access Tokens → New Token, named\n'
                '  for this overlay. Central publishes no token API, so its web\n'
                '  console is the only thing that can make one and nothing here can\n'
                '  mint a successor: re-running this command with a token created\n'
                '  there is the whole of a rotation, and the superseded token is\n'
                '  deleted on the same page.\n'
                '  The token carries the whole Central account — the network, its\n'
                '  members and its flow rules — because Central offers no narrower\n'
                '  scope. That excess is why it is delivered straight into the one\n'
                '  stack that uses it rather than kept anywhere else.\n'
                "  Which of the account's networks is this site's overlay is not\n"
                '  asked for: the id is an identity rather than a setting, so it is\n'
                '  a constant in `conventions.overlay` (rfc-002 §11).'
            ),
            fields=(Field('api-token', 'zerotierApiToken', 'the token the console showed once'),),
        ),
        Device(
            member=GITHUB_ADMIN,
            register='GitHub admin token',
            title='the GitHub admin token',
            stack=GITHUB_STACK,
            holds='the admin token the forge is declared with',
            console=(
                'github.com → Settings → Developer settings → Personal access\n'
                '  tokens → Fine-grained tokens → Generate new token.\n'
                f'  Resource owner: {conventions.forge.ACCOUNT.login}.\n'
                '  Repository access: Only select repositories —\n'
                + ''.join(f'    {repository.full_name}\n' for repository in conventions.forge.REPOSITORIES)
                + '  Repository permissions — the set credentials.md §3 defines, every\n'
                '  permission a call by the `github` stack or a secret push needs:\n'
                + ''.join(f'    {name}: {access}\n' for name, access in GITHUB_ADMIN_PERMISSIONS)
                + '  A gap in what the token was given shows as a `403 Resource not\n'
                '  accessible by personal access token`, as a GraphQL error on the\n'
                '  branch protection, as a 404 from a private repository left out\n'
                '  of the selection, or, for Contents, as a diff on the merge\n'
                '  settings that never clears.\n'
                '  GitHub publishes no API that creates a personal access token, so\n'
                '  this page is the only thing that can make one and nothing here\n'
                '  can mint a successor: re-running this command with a token made\n'
                '  there is the whole of a rotation, and the superseded token is\n'
                '  deleted on the same page.\n'
                '  It administers those repositories — settings, contents, branch\n'
                '  protection, Environments — and so can unguard `main`, which is\n'
                '  why no workflow names the `github` stack at all\n'
                '  (framework/github.md §1).\n'
                '  Which account it belongs to is not asked for: that is\n'
                '  `conventions.forge.ACCOUNT`, and the stack declares against it.'
            ),
            fields=(Field('token', 'githubAdminToken', 'the personal access token'),),
        ),
        Device(
            member=BGP,
            register='BGP session password',
            title='the BGP session password',
            stack=PHYSICAL_STACK,
            holds="the routing session's password",
            console=(
                'No console makes this one. An MD5 session password is a shared\n'
                '  secret both ends of the gateway↔worker BGP session are configured\n'
                '  with, and either end accepts any string: draw one -- `openssl rand\n'
                '  -base64 24` -- and hand it in. The stack writes it into the routing\n'
                "  daemon's configuration on the device (physical/gateway.md §1.3);\n"
                "  the worker's half is Cilium's BGPv2 `authSecretRef`, this same\n"
                '  value sealed to the cluster into `k8s-base` -- by this command once\n'
                '  the cluster exists, and by `seal` for a value recorded before it\n'
                '  did (declarative/cluster-infra.md §2). Rotating it is a fresh draw\n'
                '  and this command again, then both ends re-applied: the session is\n'
                '  down from the first apply to the second.'
            ),
            fields=(Field('password', 'gatewayBgpPassword', 'the session password'),),
            sealed=conventions.sealed.BGP_PASSWORD,
        ),
    )
}


def announce(device: Device) -> None:
    """Print the steps that create the credential, before anything is asked for.

    Always, rather than only when a prompt follows: the steps are the
    register's answer to "where does this come from", and a run that supplies
    the value from a file is exactly the run whose operator has not just read
    them.
    """
    log.warning('%s is neither minted nor derived; it comes from here:', device.title)
    for line in device.console.splitlines():
        log.warning('  %s', line)


def _collect(
    title: str, fields: tuple[Field, ...], given: Mapping[str, str | None] | None, prompt: Prompt
) -> dict[Field, str]:
    """Every field's value, from `given` or typed in, before anything is pushed.

    So an answer left blank at the second prompt costs a re-run rather than a
    half-filled slot. `given` is keyed by field name, which is what the command
    line can build from the same table (`cli`). A key naming no field is
    refused rather than dropped: dropping it turns a scripted run into an
    interactive one at the prompt it was meant to answer.
    """
    handed = given or {}
    unknown = sorted(set(handed) - {field.name for field in fields})
    if unknown:
        raise KdbxError(f'{title} has no field named {", ".join(unknown)}')
    return {field: field.resolve(handed.get(field.name), prompt=prompt, title=title) for field in fields}


def deliver(
    device: Device,
    *,
    stack: pulumi_config.Stack,
    given: Mapping[str, str | None] | None = None,
    prompt: Prompt = input,
    sealer: sealing.Sealer | None = None,
) -> tuple[str, ...]:
    """Print the console steps, collect the values, push them. Returns the keys written.

    For a row the cluster needs too (`Device.sealed`), `sealer` seals the
    values in the same run: before the configuration is written, so a cluster
    that refuses the seal leaves both slots as they were, and written after
    it. `None` is a run with no cluster to seal to yet, which delivers the
    configuration alone and names `seal` as the step that follows once there
    is one; a sealed row's `sealer` is the caller's to build, and a caller
    that cannot reach a cluster that exists refuses rather than passing
    `None`.
    """
    announce(device)
    values = _collect(device.title, device.fields, given, prompt)
    sealed: tuple[sealing.Sealer, conventions.sealed.SealedValue, dict[str, str]] | None = None
    if device.sealed is not None and sealer is not None:
        ciphertexts = sealer.seal(device.sealed, {field.name: value for field, value in values.items()})
        _ = sealer.stack(device.sealed)
        sealed = (sealer, device.sealed, ciphertexts)

    stack.fill(
        secret={field.key: value for field, value in values.items() if field.secret},
        plain={field.key: value for field, value in values.items() if not field.secret},
        holds=device.holds,
    )
    if sealed is not None:
        writer, value, ciphertexts = sealed
        writer.write(value, ciphertexts)
    elif device.sealed is not None:
        log.warning(
            'no cluster to seal %s to yet, so its sealed copy for the %s stack is not written; once the '
            "cluster's sealed-secrets controller runs, `credentials derived %s seal` writes it",
            device.title,
            device.sealed.stack,
            device.member,
        )
    return tuple(field.key for field in values)


def seal(device: Device, *, stack: pulumi_config.Stack, sealer: sealing.Sealer) -> None:
    """Seal what `stack` already holds for a row the cluster needs too, and write it where its declaring stack reads it.

    The step after a `record` that ran before there was a cluster: the value
    is read back out of the configuration that holds it rather than typed in
    again, so the two ends of the credential are one value. Every run writes
    fresh ciphertext (`sealing.py`).
    """
    if device.sealed is None:
        raise KdbxError(f'{device.title} has no copy in the cluster, so there is nothing to seal')
    values: dict[str, str] = {}
    for field in device.fields:
        try:
            value = stack.get(field.key).strip()
        except pulumi_config.PassphraseMissing:
            raise
        except pulumi_config.SlotRefused as exc:
            raise pulumi_config.SlotRefused(
                f"{device.title} is not in the {device.stack} stack's configuration: "
                f'`credentials derived {device.member} record` is what puts it there ({exc})'
            ) from exc
        if not value:
            raise pulumi_config.SlotRefused(
                f'{device.title} decrypts to an empty value in the {device.stack} stack; '
                f're-run `credentials derived {device.member} record`'
            )
        values[field.name] = value
    sealer.deliver(device.sealed, values)


@dataclass(frozen=True)
class SealedRecord:
    """One §3 row this system does not produce, whose only delivery is a value sealed to the cluster.

    A person makes it in a console, or it is carried from the legacy cluster,
    which generated it; `made` and `taken` say which. No stack's configuration
    holds it as a secret: its one consumer is in the cluster, so the seal is
    the whole of the delivery, written where the stack its sealed value names
    reads it.
    """

    #: The `credentials derived <member> record` row name, which is also the
    #: name this row carries in the slot map.
    member: str
    #: The §3 "Credential" cell, verbatim.
    register: str
    title: str
    console: str
    #: Each field is sealed under its `key`, a data key of `sealed`.
    fields: tuple[Field, ...]
    sealed: conventions.sealed.SealedValue
    #: How the value comes to exist, for the command's help.
    made: str = 'made by a person'
    #: How `record` takes it, for its help and the slot map's description.
    taken: str = 'typed in'
    #: How the value is replaced, for the command's description.
    rotation: str = 'Rotating it is the same sequence with a fresh value.'
    #: Checks the values, by data key, before anything is sealed, refusing
    #: what the row must not carry; a refusal leaves the stack as it was.
    verify: Callable[[Mapping[str, str]], None] | None = None


def matches_published_dkim(published: str, *, key: str) -> Callable[[Mapping[str, str]], None]:
    """A `SealedRecord.verify` refusing a private key at `key` unless its public half is the one `published` carries.

    `published` is a DKIM TXT record, `v=DKIM1; ...; p=<base64 DER>`, the form
    `conventions.dns` holds it in. Both halves are compared as the SHA-256 of
    the DER SubjectPublicKeyInfo, the digest sources-of-truth.md §X takes of
    the legacy Secret, each zone's TXT and the constant, so a refusal prints
    two digests of public keys and nothing of the private one.
    """
    expected = _spki_digest(_published_key(published))

    def verify(values: Mapping[str, str]) -> None:
        try:
            private = serialization.load_pem_private_key(values[key].encode(), password=None)
        except (ValueError, TypeError, UnsupportedAlgorithm):
            # `from None`: what the parser says about the input is the
            # input's business, and the input is the private key.
            raise pulumi_config.SlotRefused(
                f'{key} is not an unencrypted PEM private key, so it was not sealed'
            ) from None
        der = private.public_key().public_bytes(
            serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
        )
        actual = _spki_digest(der)
        if actual != expected:
            raise pulumi_config.SlotRefused(
                f"{key}'s public key has the digest {actual}, and the one the mail zones publish has {expected}: "
                'it is not the key they publish, so it was not sealed'
            )

    return verify


def _published_key(published: str) -> bytes:
    """The DER public key a DKIM TXT record carries in its `p=` tag."""
    tags = {name.strip(): value.strip() for name, _, value in (tag.partition('=') for tag in published.split(';'))}
    return base64.b64decode(tags['p'], validate=True)


def _spki_digest(der: bytes) -> str:
    return hashlib.sha256(der).hexdigest()


#: The rows whose delivery is a seal alone, by member.
SEALED_RECORDS: dict[str, SealedRecord] = {
    record.member: record
    for record in (
        SealedRecord(
            member='alert-webhook',
            register='Alertmanager webhook URL',
            title="alertmanager's Home Assistant webhook URL",
            console=(
                'Home Assistant → Settings → Automations → the alertmanager intake\n'
                '  automation, created by the operator against the contract\n'
                '  rfc-007 §7.3 gives: it reads the tier, the summary and the playbook\n'
                "  off each alert's labels and annotations and pushes under the same\n"
                "  title convention as the ops repository's handler → its webhook\n"
                '  trigger, which shows the full URL. A second automation rather than\n'
                "  the ops repository's own, whose body is that repository's payload.\n"
                '  Nothing mints this and nothing derives it: rotating it is a new\n'
                '  webhook id there and one more run of this command.'
            ),
            fields=(Field('url', 'url', 'the webhook URL'),),
            sealed=conventions.sealed.ALERT_WEBHOOK,
        ),
        SealedRecord(
            member='dkim-exim',
            register='DKIM private key (exim)',
            title="the mail relay's DKIM private key",
            console=(
                'The legacy cluster holds it: the Secret cert-dkim-exim in mail-system,\n'
                '  minted there by cert-manager and pinned with rotationPolicy: Never.\n'
                '  Piped straight from it, so it touches no file and no terminal:\n'
                '    kubectl --context <legacy> -n mail-system get secret cert-dkim-exim \\\n'
                "      -o jsonpath='{.data.tls\\.key}' | base64 -d \\\n"
                '      | credentials derived dkim-exim record\n'
                '  Once, after k8s-base runs the sealed-secrets controller and before\n'
                '  exim moves to the new cluster. A key whose public half is not the one\n'
                '  the mail zones publish is refused before anything is sealed.'
            ),
            fields=(Field('key', 'tls.key', 'the PEM private key', multiline=True),),
            sealed=conventions.sealed.DKIM_EXIM,
            made='carried from the legacy cluster, whose cert-manager generated it',
            taken='piped in',
            rotation=(
                'It is never rotated in place: a new key is minted in the cluster under a new selector, '
                'published beside `k8s` before any mail is signed with it.'
            ),
            verify=matches_published_dkim(conventions.DKIM_K8S, key='tls.key'),
        ),
    )
}


def record_sealed(
    record: SealedRecord,
    *,
    sealer: sealing.Sealer,
    given: Mapping[str, str | None] | None = None,
    prompt: Prompt = input,
) -> None:
    """Print the console steps, collect the values, check them, seal them into the stack that declares them."""
    log.warning('%s is neither minted nor derived; it comes from here:', record.title)
    for line in record.console.splitlines():
        log.warning('  %s', line)
    values = _collect(record.title, record.fields, given, prompt)
    data = {field.key: value for field, value in values.items()}
    if record.verify is not None:
        record.verify(data)
    sealer.deliver(record.sealed, data)


def borrow(device: Device, *, stack: pulumi_config.Stack) -> str:
    """One row's single secret, read back out of the stack that holds it.

    The other direction of `deliver`, for the one thing a row here is used for
    besides being read by its stack: every `credentials` command that pushes a
    GitHub secret — `derived sync`, `derived drill-age-identity generate`, and
    the mints that push their own carriers — authenticates to the forge as the
    GitHub admin token before it pushes anything (`github_secrets.py`), and the
    token's home is this stack's committed configuration like any other
    provider credential.

    Restricted to a single-secret row, because a caller that authenticates with
    one wants *the* credential and not a bag: a row with two would have to say
    which, and none of the rows that has two is read back at all.

    A stack that has no such key is the mid-crossing state — a checkout whose
    stack file predates the row — so the refusal names the command that fills
    it rather than passing `pulumi`'s own message on, which says to set the key
    by hand. A machine that cannot decrypt that stack *at all* is a different
    state with a different answer, and its refusal travels unchanged.
    """
    secrets = [field for field in device.fields if field.secret]
    if len(secrets) != 1:
        raise KdbxError(f'{device.title} is {len(secrets)} secrets, so there is no single value to authenticate with')
    try:
        value = stack.get(secrets[0].key).strip()
    except pulumi_config.PassphraseMissing:
        # Not this row's problem and not this row's answer: the machine cannot
        # decrypt that stack at all, and the refusal already says what to run.
        raise
    except pulumi_config.SlotRefused as exc:
        raise pulumi_config.SlotRefused(
            f"{device.title} is not in the {device.stack} stack's configuration: "
            f'`credentials derived {device.member} record` is what puts it there ({exc})'
        ) from exc
    if not value:
        raise pulumi_config.SlotRefused(
            f'{device.title} decrypts to an empty value in the {device.stack} stack; '
            f're-run `credentials derived {device.member} record`'
        )
    return value


__all__ = (
    'BGP',
    'DEVICES',
    'GITHUB_ADMIN',
    'SEALED_RECORDS',
    'STDIN',
    'Device',
    'Field',
    'SealedRecord',
    'announce',
    'borrow',
    'deliver',
    'matches_published_dkim',
    'record_sealed',
    'seal',
)
