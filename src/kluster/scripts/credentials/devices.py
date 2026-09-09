"""The hand-made credentials (docs/credentials.md §3): made in a console, delivered to a stack.

Some of §3's rows are neither minted from a seed nor generated and escrowed.
The credential is made in the console that checks it — the appliance's own, or
the provider's where the provider publishes no API for making one — and this
side of the system only delivers it:

-   the **UniFi API key**, which the controller mints for a dedicated local
    admin and shows once;
-   the **AdGuard admin login**, which *is* the API credential — AdGuard Home
    has no scoped API at all (the security audit's M6), so the account both
    instances carry is what a rewrite call authenticates as;
-   the **ZeroTier Central API token**, which Central mints only in its own
    web console and scopes to the whole account;
-   the **GitHub admin token**, a personal access token created in the GitHub
    UI, which is what the `github` stack declares the forge with and what
    `credentials derived sync` pushes every GitHub secret as.

None of them is a seed: a seed is a credential that mints successors
(`entries.py`), and each of these mints nothing. Losing one costs a console
visit and a re-run of its `record` command, which is also the whole of its
rotation.

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

**Which stack takes a row is not an argument.** The credential authenticates
against one thing, and the stack that talks to that thing is the only consumer
there is: `physical` drives the UDM's Network API and the overlay's Central
account, `dns` writes the split-horizon rewrites on the AdGuard pair
(declarative/dns.md §3), and `github` declares the forge.

§3's other pasted row — the UDM SSH key and the libvirt identity — has no
member here, because there are no console steps to print for it: nobody
creates either in a console. The installation's other automation installs them
(§3), and the act on this side is a paste with nothing to guide.
"""

from __future__ import annotations

import getpass
import logging
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from ... import conventions
from . import derived, pulumi_config
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


@dataclass(frozen=True)
class Field:
    """One value of a device credential, and every way the command may get it."""

    #: The option stem, and the key this field is addressed by in `given`.
    name: str
    #: The Pulumi config key it is delivered into. The slot map imports it, so
    #: the map and the push cannot name different keys.
    key: str
    #: What to ask for, in the operator's words.
    describes: str
    #: A secret is encrypted into the committed file and never echoed; a plain
    #: field is an address that file may carry in the clear (§4).
    secret: bool = True

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
        if self.secret:
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
        if given is None:
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
    """One §3 row whose credential is made in the console that checks it."""

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
                "  That account is part of each instance's initial configuration,\n"
                '  which the gw-config device services declare and push (physical/\n'
                '  gateway.md §1). Changing it is a change there; this command\n'
                '  delivers whatever that configuration now says.'
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
                '  tokens → Tokens (classic) → Generate new token, scope `repo`.\n'
                '  GitHub publishes no API that creates a personal access token, so\n'
                '  this page is the only thing that can make one and nothing here\n'
                '  can mint a successor: re-running this command with a token made\n'
                '  there is the whole of a rotation, and the superseded token is\n'
                '  deleted on the same page.\n'
                "  It administers this account's repositories — branch protection,\n"
                '  rulesets, Environments and their gates — which is as narrow as\n'
                '  the scope list goes, and why no workflow names the `github`\n'
                '  stack at all (framework/github.md §1).\n'
                '  Which account it belongs to is not asked for: that is\n'
                '  `conventions.forge.ACCOUNT`, and the stack declares against it.'
            ),
            fields=(Field('token', 'githubAdminToken', 'the personal access token'),),
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


def deliver(
    device: Device,
    *,
    stack: pulumi_config.Stack,
    given: Mapping[str, str | None] | None = None,
    prompt: Prompt = input,
) -> tuple[str, ...]:
    """Print the console steps, collect the values, push them. Returns the keys written.

    Every value is collected before the first one is pushed, so an answer left
    blank at the second prompt costs a re-run rather than a half-filled slot.

    `given` is keyed by field name, which is what the command line can build
    from the same table (`cli`). A key naming no field is refused rather than
    dropped: dropping it turns a scripted run into an interactive one at the
    prompt it was meant to answer.
    """
    announce(device)
    handed = given or {}
    unknown = sorted(set(handed) - {field.name for field in device.fields})
    if unknown:
        raise KdbxError(f'{device.title} has no field named {", ".join(unknown)}')
    values = {
        field: field.resolve(handed.get(field.name), prompt=prompt, title=device.title) for field in device.fields
    }

    stack.fill(
        secret={field.key: value for field, value in values.items() if field.secret},
        plain={field.key: value for field, value in values.items() if not field.secret},
        holds=device.holds,
    )
    return tuple(field.key for field in values)


def borrow(device: Device, *, stack: pulumi_config.Stack) -> str:
    """One row's single secret, read back out of the stack that holds it.

    The other direction of `deliver`, for the one thing a row here is used for
    besides being read by its stack: `credentials derived sync` authenticates
    to the forge as the GitHub admin token before it pushes anything
    (`github_secrets.py`), and the token's home is this stack's committed
    configuration like any other provider credential.

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


__all__ = ('DEVICES', 'GITHUB_ADMIN', 'STDIN', 'Device', 'Field', 'announce', 'borrow', 'deliver')
