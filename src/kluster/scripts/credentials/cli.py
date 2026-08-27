"""`credentials` — the executable form of the credential register.

The command tree is the register's own tiers, not the accounts behind them,
and every command reads `credentials <subject> [<row>] <verb>`. There are four
subjects: `root` for the account roots a workstation borrows, `seed` for the
credentials the offline kit holds, `kit` for that kit and what is done to the
whole of it, and `derived` for the credentials minted or generated for one
consumer. A row carries one name across the tree, the slot map and the
register's tables.

Reading `credentials --help` beside the register should show the same shapes;
a command with no row, or a row with no command, is the bug that discipline is
meant to surface. Every help text below stands on its own, though: it says
what its command does and how, for a reader with no document open, and a
pointer to the register is a trailing `See also` line rather than the
explanation itself.

Every action is mint -> push to every slot -> verify, and therefore idempotent:
rotation is a re-run, not a second procedure.
"""

from __future__ import annotations

import argparse
import getpass
import logging
import sys
from pathlib import Path

from . import (
    b2,
    derived,
    devices,
    entries,
    escrow,
    github_secrets,
    lifecycle,
    masters,
    oci_iam,
    pulumi_config,
    slots,
    workstation,
)
from .age import AgeError
from .escrow import EscrowError
from .kdbx import PATH_ENV, KdbxError, KdbxStore, default_path
from .masters import CredentialRejected
from .pulumi_config import SlotRefused

log = logging.getLogger(__name__)

#: The register this tree is the executable form of, for the one line of a help
#: text that may point at it.
REGISTER = 'docs/credentials.md'


def _see_also(*sections: str) -> str:
    """The pointer a help text is allowed to spend on the register.

    Nothing but the pointer, so that deleting it deletes no explanation, and
    always in an `epilog`: argparse prints an epilog after everything else,
    which is what makes "trailing" a property a test can check rather than an
    intention (`tests/test_cli_help.py`). One per subject, because a reader who
    wants the surrounding argument wants it for the subject and not for each of
    its verbs.
    """
    return f'See also: {REGISTER} {", ".join(sections)}'


#: Which command runs when. The tree says what exists; this says what to do
#: with it, because "one subcommand per register row" answers neither "where
#: do I start" nor "is this the one that destroys something".
_ORDER = """when to run what:

  bring-up, from nothing
    0. credentials root <root> remember
         Keeps an account root -- the credential an account is administered
         with -- on this machine, once, so the mints below ask for nothing.
         Every root is looked up the same way: desktop secret store, then
         its token file, then its environment variable, then a prompt -- so
         skipping this costs a prompt rather than a failure, which is how a
         headless run works.
    1. credentials kit bootstrap
         Fills a kit with every seed, creating the kit if it is absent.
         The recovery key is one of them, and creating it also writes
         escrow/RECIPIENTS. Stops at each credential no API can create and
         prints the console steps. Re-run it to resume.
    2. credentials derived oci-state-backend mint --compartment <ocid>
         The appliance's own OCI key, minted from the seed into a
         workstation slot. The next step reads it there; nothing else does.
    3. state-backend provision
         The Pulumi state backend, which every stack needs before it can act,
         and the first thing to escrow: it generates the CA and the backup
         identities it is about to install and commits their ciphertexts.
    4. credentials derived pulumi-passphrase generate
         The one escrowed row no other command mints. It writes the
         workstation slot as well as the ciphertext, so mise.toml puts the
         passphrase in the environment of every pulumi run from here on.
    5. credentials derived cloudflare-zones mint
         Mints the zone-scoped Cloudflare token from the seed and writes
         it into the dns stack's config, which is then committed. One
         derived row per command; re-running one rotates it.
    6. credentials derived oci-physical mint --compartment <ocid>
       credentials derived b2-management mint
         The two provider credentials the physical stack runs on, into its
         config, which is then committed like the one above.
    7. credentials derived unifi record
       credentials derived adguard record
         The two credentials no API here mints: each is made on the
         device that checks it, so the command prints the steps that
         create it, takes the value without echoing it, and writes it
         into the stack config that reads it -- physical for the UniFi
         key, dns for the AdGuard login. Both files are then committed.
    8. credentials derived sync
         The GitHub secrets CI reads, for the rows whose value lives
         somewhere else and is copied into a slot. Run it again whenever one
         of those values moves; a row it cannot fill yet says which slot is
         waiting on what.

  on a workstation that develops without the kit
    Copy the .credentials directory from a machine that has one: the
    passphrase slot and the client bundle come with it. On a machine that
    does hold the kit, `credentials derived pulumi-passphrase recover`
    writes that slot once, and mise.toml reads it on every pulumi run.

  day to day
    Nothing. No runtime credential is in the kit, so no operation outside
    bring-up, rotation and the yearly offline day opens it.
    `credentials derived check` needs no kit at all.

  when one seed is lost
    credentials kit bootstrap --only <member>
         Re-creates that row alone; the rest of the kit is untouched. Not
         the recovery key: every ciphertext under escrow/ opens with that
         one and nothing else, so losing it is losing them.

  one-time repair
    credentials seed oci domain
         Records the tenancy's identity domain on an OCI row written before
         that attribute existed. Without it a rotation mints a successor and
         cannot retire what it supersedes, because the legacy delete call is
         refused. Borrows the account root, once.

  rotating one credential
    credentials derived <row> generate
         A new generation for that escrowed row alone, adopted by re-running
         what consumes it. Nothing else moves.

  rotating the kit
    credentials kit rotate --into <new kit>
         Writes a *new* database, and re-wraps every escrow ciphertext to
         the successor recovery key. No production secret changes value, so
         the retired file is destroyable once the run is verified.

  looking without changing
    credentials derived check     every expected escrow row present, every
                                  ciphertext an age file, generations dense
    credentials derived ls        every derived credential, where its value
                                  comes from, and every slot it lands in
    credentials kit ls | show <entry>
    credentials root ls           which account roots this machine holds, and
                                  which layer of the chain each comes from
    credentials kit password remember
                                  stores the kit's master password in the
                                  desktop secret store, so a long run is not
                                  guarded by a password typed into it

See also: docs/credentials.md -- §2 the seeds, §3 the derived rows, §4 this order.
"""


def _slot_source(command: argparse.ArgumentParser) -> None:
    """Say where the state backend's client bundle is.

    Every row delivered into a Pulumi config secret needs it: the stack's
    configuration lives in the state backend, so pushing into it means
    reaching the backend, which is this bundle plus the passphrase the kit
    recovers.
    """
    _ = command.add_argument(
        '--bundle-dir',
        type=Path,
        default=workstation.bundle_dir(),
        help='the client certificates `state-backend` wrote, which reach the stack config',
    )


def _oci_consumer(command: argparse.ArgumentParser) -> None:
    """The two arguments every minted OCI row takes.

    The seed the mint reads and the compartment it confines the result to are
    the same question for each consumer; where the rows differ is where the
    answer is delivered, which is what makes them separate subcommands.
    """
    _ = command.add_argument(
        '--entry',
        default=derived.OCI_SEED_ENTRY,
        help=f'the kit entry the seed is read from (default: {derived.OCI_SEED_ENTRY})',
    )
    # Required because nothing in this repository knows it: a compartment OCID
    # is a fact about the tenancy, and it is what the minted policy confines
    # the key to.
    _ = command.add_argument(
        '--compartment', required=True, metavar='<ocid>', help='the compartment the key may administer'
    )


#: What `seed <member> create` does, in the three shapes a seed comes in: minted
#: with an account root, made in a console, or the recovery key generated here.
#: Written out because the tree is generated from the register's table, so the
#: text has to be chosen from the row rather than typed beside it.
_CREATE_FROM_ROOT = 'mint it from the account root (bring-up, or seed loss)'
_CREATE_MINTED = (
    'Mint this seed with the account root and write it into the kit. The root is looked up on this '
    'machine and prompted for when it is not here. The entry is written whether or not the kit already '
    'holds one, which is what makes this the repair for a single lost seed; the walk that skips what is '
    'already there is `kit bootstrap`.'
)
_CREATE_CONSOLE = (
    'Print the steps that create this seed in a console -- its platform has no API that can -- and write '
    'what they produce into the kit: the secret itself, the public identifier that goes with it, and the '
    'file the platform hands over once, where there is one. The entry is written whether or not the kit '
    'already holds one; the walk that skips what is already there is `kit bootstrap`.'
)
_CREATE_RECOVERY = (
    'Generate the recovery keypair: the private half into the kit, the public recipient into '
    'escrow/RECIPIENTS, which is a file to commit. Every escrowed ciphertext is encrypted to that '
    'recipient and opens with nothing else, so both halves are refused if either already exists -- '
    'writing a second key here would lose every secret filed under the first. Replacing it deliberately '
    'is `kit rotate`, which re-encrypts the ciphertexts on its way.'
)


def build_parser() -> argparse.ArgumentParser:
    """The whole command tree, as data.

    Public because the tree is generated rather than written out: a test walks
    it and drives every leaf through `main`, so a register row that `main`
    cannot dispatch fails there rather than on an operator's first run. A
    second test walks it to render every `--help`, holding each against the
    rule that a help text explains itself (`tests/test_cli_help.py`).
    """
    parser = argparse.ArgumentParser(
        prog='credentials',
        description=__doc__,
        epilog=_ORDER,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    _ = parser.add_argument(
        '--kdbx',
        type=Path,
        default=None,
        help=f'the offline kit to open (default: ${PATH_ENV}, else {workstation.kit_path()})',
    )
    _ = parser.add_argument(
        '--escrow',
        type=Path,
        default=None,
        help=f'where the escrowed ciphertexts are filed (default: {escrow.DIRECTORY}/ in this checkout)',
    )
    subjects = parser.add_subparsers(dest='subject', required=True, metavar='<subject>')

    # The account roots are not in the kit and not in any database this
    # repository opens: each is looked up through one chain -- desktop secret
    # store, token file, environment variable, prompt (`masters.py`). This
    # subject is how they get onto a machine, and the only thing that writes
    # them.
    root_subject = subjects.add_parser(
        'root',
        help='the credentials the provider accounts themselves are administered with',
        description=(
            'An account root is the credential an account is administered with. Nothing here mints one: '
            'each is made once in a provider console, and it exists so that a seed can be minted without '
            'it afterwards. Roots stay out of the kit, and every one of them is found the same way, first '
            'hit wins -- the desktop secret store, then a token file under the checkout, then an '
            'environment variable, then a prompt. `remember` fills the two layers this machine can write, '
            'so a mint that needs a root asks for nothing; a root nobody remembered costs a prompt rather '
            'than a failure, which is what makes a headless run possible.'
        ),
        epilog=_see_also('§2'),
    )
    roots = root_subject.add_subparsers(dest='member', required=True, metavar='<root>')
    root_listing = roots.add_parser(
        'ls',
        help='which roots this machine holds, and from which layer; prints no values',
        description=(
            'For every account root, name the layer each of its fields would be answered from -- the '
            'secret store, a token file or the environment -- or list the fields no layer holds at all. '
            'This is the answer to "will the next run stop and ask me something"; nothing is printed but '
            'those layer names.'
        ),
    )
    root_listing.set_defaults(action='ls')
    for account in masters.ROOTS.values():
        described = [field.describes for field in account.fields]
        made_of = ' and '.join([', '.join(described[:-1]), described[-1]] if len(described) > 1 else described)
        root_cmd = roots.add_parser(
            account.member,
            help=account.title,
            description=(
                f'{account.title}, which is made of {made_of}. It is created in the account console and '
                'minted by nothing here, so these two verbs decide only whether this machine keeps a copy '
                'of it.'
            ),
        )
        root_verbs = root_cmd.add_subparsers(dest='action', required=True, metavar='<verb>')
        _ = root_verbs.add_parser(
            'remember',
            help='prompt for it and keep it on this machine',
            description=(
                'Print the console steps that create this root, ask for each of its fields, and keep each '
                'one where its readers can reach it: the desktop secret store, or a `0600` file under the '
                'checkout for a field a template has to read on its own -- and for every field on a '
                'machine with no secret store at all. A value is kept because it was asked for here, '
                'never as a side effect of a run that happened to read it.'
            ),
        )
        _ = root_verbs.add_parser(
            'forget',
            help='remove it from the secret store and from its token file',
            description=(
                'Remove every field of this root from both layers this machine can write, since '
                '`remember` may have used either. An environment variable belongs to the shell that set '
                'it and is left alone. Finding nothing to remove is an error rather than a quiet success, '
                'so a mistyped root name says so.'
            ),
        )

    # The tree is generated from the register's table rather than written out,
    # so a seed that exists in the register and nowhere in the code shows up as
    # a subcommand that refuses to run -- not as a subcommand that is missing.
    seed_subject = subjects.add_parser(
        'seed',
        help='the long-lived credentials the offline kit holds, one member per row',
        description=(
            'A seed is a credential kept in the offline kit so that everything a stack runs on can be '
            'minted from it without reaching for an account root: one per provider, plus the recovery key '
            'the escrowed secrets are encrypted to. Each member below is one entry of the kit. `create` '
            'writes it for the first time -- minting it where the platform has an API for that, printing '
            'the console steps where it has none -- and a seed that can mint its own successor also has '
            '`rotate`. A member listed here whose mint is not written refuses by name, so the register '
            'and the tree can be read against each other.'
        ),
        epilog=_see_also('§2'),
    )
    members = seed_subject.add_subparsers(dest='member', required=True, metavar='<member>')
    for seed in entries.SEEDS.values():
        member = members.add_parser(
            seed.member,
            help=f'mints {seed.mints}',
            description=(
                f'The kit entry `{seed.entry}`: its UserName holds {seed.identifier} and its Password '
                f'holds the secret itself. It mints {seed.mints}. '
                + (
                    'It is generated here rather than obtained from anywhere, and its private half never '
                    'leaves the kit.'
                    if seed.member == 'recovery'
                    else (
                        'No API of that platform creates this one, so `create` prints the steps that make '
                        'it in a console and takes what they produce.'
                        if seed.manual
                        else 'Its platform can mint it, so no console visit is involved.'
                    )
                )
            ),
        )
        _ = member.add_argument(
            '--entry', default=seed.entry, help=f'the kit entry to read or write instead (default: {seed.entry})'
        )
        seed_verbs = member.add_subparsers(dest='action', required=True, metavar='<verb>')
        _ = seed_verbs.add_parser(
            'create',
            help='generate it here (bring-up, once)'
            if seed.member == 'recovery'
            else ('record the one a console makes (bring-up, or seed loss)' if seed.manual else _CREATE_FROM_ROOT),
            description=_CREATE_RECOVERY
            if seed.member == 'recovery'
            else (_CREATE_CONSOLE if seed.manual else _CREATE_MINTED),
        )
        if seed.self_reproducing:
            _ = seed_verbs.add_parser(
                'rotate',
                help='have the seed mint and install its successor',
                description=(
                    'Sign as the seed itself to mint its replacement, write the replacement into the kit, '
                    'and retire the predecessor. The account root is not involved: a seed that can create '
                    'its own kind is exactly a seed that can rotate without one. Credentials already '
                    'minted from the predecessor keep working -- each is replaced by re-running its own '
                    'command.'
                ),
            )
        if seed.repair is not None:
            _ = seed_verbs.add_parser(seed.repair.verb, help=seed.repair.summary, description=seed.repair.detail)

    # The offline store itself, and the things done to the whole of it. The two
    # walks take the seed table in order and skip what is already there, so an
    # interrupted run is resumed by re-running it rather than by remembering
    # where it stopped.
    kit_subject = subjects.add_parser(
        'kit',
        help='the offline store, and what is done to the whole of it',
        description=(
            'The kit is one KeePassXC database, kept offline, holding every seed and the recovery key the '
            'escrowed secrets are encrypted to -- and nothing a running cluster needs, which is why no '
            'day-to-day operation opens it. `--kdbx` or $KLUSTER_KDBX says where it is. The verbs here act '
            'on the whole of it: `bootstrap` fills one, `rotate` writes a successor, `rewrap` re-encrypts '
            'the escrow to the key it holds, and `ls`/`show` read it without disclosing a secret. The '
            'database is unlocked once per run, from the desktop secret store when `password remember` '
            'put it there and from a prompt otherwise.'
        ),
        epilog=_see_also('§2.1', '§4'),
    )
    kit_verbs = kit_subject.add_subparsers(dest='member', required=True, metavar='<verb>')

    boot = kit_verbs.add_parser(
        'bootstrap',
        help='fill a kit with every seed; creates the kit if absent',
        description=(
            'Create every seed the kit does not hold yet, and the kit itself if there is none. A row a platform '
            'can mint is minted; the rest stop and print the console steps that create them, so an interrupted '
            'run is resumed by running this again. Creating the recovery row also writes the public half of the '
            'recovery key to escrow/RECIPIENTS, which is a file to commit.'
        ),
    )
    boot.set_defaults(action='bootstrap')
    _ = boot.add_argument(
        '--only',
        default=None,
        metavar='<member>',
        help='create just this seed; `--only recovery` is how a kit that predates the escrow gets its key',
    )

    rot = kit_verbs.add_parser(
        'rotate',
        help='write a new kit in which every seed is replaced',
        description=(
            'Write a second database in which every seed has been replaced, leaving the one in hand '
            'untouched: a seed whose platform can mint its successor does so, and the rest print their '
            'console steps exactly as at bootstrap. Rotating the recovery key re-encrypts every escrowed '
            'ciphertext to the successor, so no production secret changes value and nothing has to be '
            're-deployed -- but the retired database opens the escrow no longer, which is what makes it '
            'destroyable once this run has been verified.'
        ),
    )
    rot.set_defaults(action='rotate')
    _ = rot.add_argument(
        '--into', type=Path, required=True, help='where to write the successor kit; must not exist yet'
    )
    _ = rot.add_argument('--only', default=None, metavar='<member>', help='rotate just this seed into the successor')

    rewrap = kit_verbs.add_parser(
        'rewrap',
        help='re-encrypt every escrowed ciphertext to the recipients now on file',
        description=(
            'Open every ciphertext under escrow/ with the recovery key this kit holds, and write each back '
            'encrypted to whatever escrow/RECIPIENTS already names. No plaintext changes, so no consumer is '
            'touched. `kit rotate` does this for itself; running it alone finishes a rotation that died part '
            'way through, or takes in a ciphertext written while the file already named the successor. A run '
            'that would leave the registry with nothing in hand able to open it is refused.'
        ),
    )
    rewrap.set_defaults(action='rewrap')

    kit_listing = kit_verbs.add_parser(
        'ls',
        help='list the entry paths this kit holds',
        description=(
            'Print the path of every entry in the kit, one per line, which is how a row is addressed '
            'everywhere else in this tree. Opening the database is all this needs; no field of any entry '
            'is read, so nothing secret can be printed.'
        ),
    )
    kit_listing.set_defaults(action='ls')
    _ = kit_listing.add_argument(
        'group', nargs='?', default='/', help='list this group alone, e.g. `seeds` (default: the whole kit)'
    )
    show = kit_verbs.add_parser(
        'show',
        help="an entry's non-secret fields",
        description=(
            "Print one entry's Title, UserName, URL and Notes -- the fields that are not the secret. It is "
            'the way to check that a row holds the identifier it should (a key id, an OCID, an age '
            'recipient) without disclosing anything. The password, the attachments and the protected '
            'attributes are not printed by any command here.'
        ),
    )
    show.set_defaults(action='show')
    _ = show.add_argument('entry', help='the entry path, as `kit ls` prints it')

    # The kit is the one database a run opens, and a bring-up holds it open
    # for minutes; guarding that with a password typed into an unwatched
    # process is what this avoids.
    password = kit_verbs.add_parser(
        'password',
        help="this database's master password, as this machine holds it",
        description=(
            "The kit's own master password, which every command here has to have before it can read a "
            'row. It is asked for once per run; remembering it in the desktop secret store means a long '
            'bring-up is not guarded by a password typed into an unwatched terminal. This is about this '
            'machine alone -- neither verb changes the password the database is encrypted with.'
        ),
    )
    password_verbs = password.add_subparsers(dest='action', required=True, metavar='<verb>')
    _ = password_verbs.add_parser(
        'remember',
        help="store this database's master password in the desktop secret store",
        description=(
            'Ask for the master password, prove it opens this database, and only then store it under the '
            "database's path in the desktop secret store. Proving it first is the point: a remembered "
            'password that does not work is worse than none, because every later run tries it before '
            'falling back to the prompt.'
        ),
    )
    _ = password_verbs.add_parser(
        'forget',
        help='remove it from the secret store',
        description=(
            "Drop this machine's stored copy of the master password. The database is untouched and still "
            'opens with the same password; the next run asks for it again.'
        ),
    )

    # The other half of the register: the per-consumer rows. Each is named here
    # exactly as the slot map names it (`slots.py`), so a row has one spelling
    # across the tree, the map and the register's tables. What differs between
    # rows is the verb, because what differs between them is how the value
    # comes into being: `mint` for a row a seed mints, `generate`/`import`/
    # `recover` for a row generated here and escrowed, `record` for a row made
    # on a device of the estate and typed in.
    #
    # A row joins the tree when its consumer exists -- a mint with nowhere to
    # deliver would park a secret, which is the one thing the register forbids
    # outright.
    derived_subject = subjects.add_parser(
        'derived',
        help='the credentials each consumer runs on, one row each, and the map of them',
        description=(
            'One row per credential a consumer actually runs on, named here as the slot map names it. The '
            'verb a row has says how its value comes into being. `mint` asks a platform for a fresh '
            "credential, signing as the seed, and writes it into the consumer's slot in the same run -- "
            'so re-running one is how it is rotated. `generate`, `import` and `recover` belong to a row '
            'nothing external can mint: it is random, made here, and escrowed -- encrypted to the '
            "recovery key's public half and committed as a ciphertext under escrow/. `record` belongs to "
            'a row made on a device of the estate: it prints the steps, takes the value without echoing '
            'it, and delivers it. `ls`, `check` and `sync` act on the map rather than on one row.'
        ),
        epilog=_see_also('§3'),
    )
    rows = derived_subject.add_subparsers(dest='member', required=True, metavar='<row>')

    # Three verbs about the whole map rather than about one row of it.
    derived_listing = rows.add_parser(
        'ls',
        help='the whole slot map; needs no kit, no token and no network',
        description=(
            'Print every row of the map: where its value comes from, every slot it is delivered into, and '
            'what is still missing for the rows whose consumer is not built yet. The map is a checked-in '
            'file, so this reads no credential of any kind -- it works in a fresh clone, with no kit, no '
            'token and no network.'
        ),
    )
    derived_listing.set_defaults(action='ls')
    checking = rows.add_parser(
        'check',
        help='what the escrow is missing or holds malformed; needs no kit',
        description=(
            'Hold the escrow/ directory against the rows that are expected to be in it: a ciphertext for '
            'every escrowed row, each one really an age file, and generations numbered densely from the '
            'first. Every problem is reported rather than the first, because a registry is checked to '
            'learn what is wrong with it. Nothing is decrypted, so this needs no kit -- it is the one '
            'command a stranger to the system can run.'
        ),
    )
    checking.set_defaults(action='check')
    syncing = rows.add_parser(
        'sync',
        help='copy into their GitHub slots the rows whose value lives elsewhere',
        description=(
            'Fill the GitHub secrets of the rows whose value is a copy of something that lives somewhere else: '
            'a value generated inside a stack and read back out of its state, and a value that is typed in '
            'because this slot is the only place it is stored. Resolve, push, verify, per row, so a first fill '
            'and a refill after a channel is lost are one command. '
            'A row born into its slot is out of scope: a minted credential is disclosed once, to the call that '
            'creates it, so its own `mint` fills its slot in the same run and asking again would produce a '
            'different credential. Naming one is refused rather than silently doing nothing.'
        ),
    )
    syncing.set_defaults(action='sync')
    _ = syncing.add_argument(
        '--only', default=None, metavar='<row>', help='one row of the map; `derived ls` names them'
    )
    _slot_source(syncing)

    # In bring-up order, which is the order an operator meets them in.
    #
    # Only the zones row takes a `--stack`. What each of the others mints is
    # named after the row and the mint retires everything else of that name, so
    # a delivery aimed elsewhere would revoke the real stack's live credential
    # on its way to filling another stack's slot (`derived.py`).
    appliance_key = rows.add_parser(
        derived.OCI_STATE_BACKEND_ROW,
        help="the state-backend appliance's own OCI key",
        description=(
            'The OCI credential the state-backend appliance is provisioned with. It is the one row that '
            'is not delivered into a stack: its consumer is what builds the backend every stack keeps its '
            'configuration in, so it runs before there is anywhere else to put a secret.'
        ),
    )
    appliance_verbs = appliance_key.add_subparsers(dest='action', required=True, metavar='<verb>')
    appliance_mint = appliance_verbs.add_parser(
        'mint',
        help='mint the user, group, policy and key from the seed into a workstation slot',
        description=(
            'Sign as the OCI seed to create a user, its group, its policy and its API key, confined to '
            'the compartment named below, and write the signing configuration into a `0600` file under '
            'the checkout. `state-backend provision` reads it from there; nothing else does, and it '
            'never leaves this machine. A previous key of the same name is retired once the new one '
            'answers, so re-running this is the rotation.'
        ),
    )
    _oci_consumer(appliance_mint)

    zones_row = rows.add_parser(
        derived.ZONES_ROW,
        help='the zone-scoped Cloudflare provider token',
        description=(
            "The token the DNS stack's Cloudflare provider signs with. It carries record edit on the "
            "estate's zones and nothing else, so widening it means adding a zone to the estate's list and "
            'running the mint again.'
        ),
    )
    zones_verbs = zones_row.add_subparsers(dest='action', required=True, metavar='<verb>')
    zones_mint = zones_verbs.add_parser(
        'mint',
        help="mint it from the seed into the dns stack's config secret, with the account id beside it",
        description=(
            'Open the Cloudflare seed in the kit, look up the ids of the zones this estate owns, mint a '
            'token scoped to exactly those, and write it into the stack config as an encrypted value -- '
            'with the account id beside it in the clear, which the program needs and which is no secret. '
            'The push is read back before the run succeeds, and the committed file is the delivery, so '
            'the change has to be committed afterwards. A live token of the same name is retired once '
            'its successor is verified.'
        ),
    )
    _ = zones_mint.add_argument(
        '--entry',
        default=derived.CLOUDFLARE_SEED_ENTRY,
        help=f'the kit entry the seed is read from (default: {derived.CLOUDFLARE_SEED_ENTRY})',
    )
    _ = zones_mint.add_argument(
        '--stack',
        default=derived.ZONES_STACK,
        help=f'the stack whose config takes the token (default: {derived.ZONES_STACK})',
    )
    _slot_source(zones_mint)

    physical_key = rows.add_parser(
        derived.OCI_PHYSICAL_ROW,
        help="the physical stack's OCI user and API key",
        description=(
            'The OCI credential the physical stack acts with: its own IAM user, administering one '
            'compartment and a stranger outside it. Which stack takes it is not a choice -- the user is '
            'named after this row, and minting retires every other key of that name.'
        ),
    )
    physical_verbs = physical_key.add_subparsers(dest='action', required=True, metavar='<verb>')
    physical_mint = physical_verbs.add_parser(
        'mint',
        help="mint the user, group, policy and key from the seed into that stack's config secrets",
        description=(
            'Sign as the OCI seed to create the user, its group and the policy that confines it to the '
            "compartment named below, then write the signing configuration into the stack's committed "
            'config: the tenancy, the user, the fingerprint and the private key encrypted, the region and '
            'the compartment in the clear because the program reads them that way. The compartment '
            'travels with the credential because a key says what it may sign, not where it may act. '
            'Commit the config afterwards; re-running this is the rotation.'
        ),
    )
    _oci_consumer(physical_mint)
    _slot_source(physical_mint)

    management_key = rows.add_parser(
        derived.B2_MANAGEMENT_ROW,
        help='the B2 bucket and lifecycle admin key',
        description=(
            'The B2 key the physical stack administers buckets and their lifecycle rules with. It carries '
            'no file capability at all: the credential that manages the backup buckets cannot read a byte '
            'out of them.'
        ),
    )
    management_verbs = management_key.add_subparsers(dest='action', required=True, metavar='<verb>')
    management_mint = management_verbs.add_parser(
        'mint',
        help="mint it from the seed into the physical stack's config secret",
        description=(
            'Sign as the B2 seed to create a fresh management key, and write both halves into the stack '
            'config as encrypted values -- the id names the one key of that name the account holds, and '
            'the pair is one credential. The key of that name that was live before is retired once the '
            'successor is verified, so re-running this is the rotation. Commit the config afterwards.'
        ),
    )
    _ = management_mint.add_argument(
        '--entry',
        default=derived.B2_SEED_ENTRY,
        help=f'the kit entry the seed is read from (default: {derived.B2_SEED_ENTRY})',
    )
    _slot_source(management_mint)

    # The rows whose credential is made on a device of the estate rather than
    # minted from a seed. `record` prints the console steps that create it,
    # takes the value without echoing it and pushes it into the stack that
    # reads it (`devices.py`); nothing here mints anything, so the delivery is
    # the whole of the act.
    for device in devices.DEVICES.values():
        device_row = rows.add_parser(
            device.member,
            help=f'{device.title}, made on the device itself',
            description=(
                f'Nothing here mints {device.title}: it is made on the appliance that checks it, and this '
                f'side of the system only delivers it into the {device.stack} stack, which is the one '
                'consumer that talks to that appliance.'
            ),
        )
        device_verbs = device_row.add_subparsers(dest='action', required=True, metavar='<verb>')
        record = device_verbs.add_parser(
            'record',
            help=f"take it from the console into the {device.stack} stack's config",
            description=(
                'Print the steps that create this credential, take each of its values without echoing a '
                f"secret, and write them into the {device.stack} stack's committed config, reading them "
                'back to prove the push landed. Rotating it is the same command with a fresh value from '
                'the same console. Commit the config afterwards.'
            ),
        )
        for field in device.fields:
            _ = record.add_argument(
                field.flag,
                default=None,
                metavar='<path>' if field.secret else '<value>',
                help=(
                    f'read {field.describes} from a file rather than a prompt (`{devices.STDIN}` reads stdin)'
                    if field.secret
                    else f'{field.describes}, rather than a prompt'
                ),
            )
        _slot_source(record)

    # The escrowed rows: random secrets no provider mints, whose ciphertexts
    # are committed and open with the one recovery key the kit holds.
    # Generating and escrowing are a single act, so no verb here hands out a
    # secret the registry does not carry. The row's escrow label travels on the
    # namespace rather than as an argument -- the directory keeps its `/`
    # paths, and the tree names the row the way every other row is named
    # (`escrow.row_name`).
    for name, label in escrow.rows().items():
        escrowed = rows.add_parser(
            name,
            help=f'{label.what} (escrowed)',
            description=(
                f'This row holds {label.what}. No provider mints it: it is random, made here, and '
                'escrowed in the same act -- encrypted to the recovery key on file and committed as a '
                'ciphertext under escrow/, so a value this command hands back always has a copy someone '
                'with the kit can open. Filed by generation, newest last, and nothing adopts a new '
                'generation on its own.'
            ),
        )
        escrowed.set_defaults(label=label.name)
        escrow_verbs = escrowed.add_subparsers(dest='action', required=True, metavar='<verb>')
        _ = escrow_verbs.add_parser(
            'generate',
            help="draw this row's next generation and escrow it",
            description=(
                'Draw a fresh random secret, check it against the shape this row is supposed to hold, and '
                'write its ciphertext before handing it to anything -- so no run can leave a generated '
                'secret the escrow does not carry. It becomes the next generation; the previous one is '
                'still what production holds until whatever consumes this row is re-run against the new '
                'one, which is what makes rotating a credential a decision rather than a side effect. '
                'The new ciphertext is a file to commit.'
            ),
        )
        adopt = escrow_verbs.add_parser(
            'import',
            help="escrow a value that already exists as this row's next generation",
            description=(
                "Take a secret that already exists and file it as this row's next generation, checked "
                'against the shape the row holds before anything is written. This is how a credential '
                'that predates the escrow is taken on without rotating it, and the next generation rather '
                'than a fixed first one so an import can never overwrite what is already filed. The value '
                'is read from standard input, never from an argument another process could read out of '
                'the process table. The ciphertext is a file to commit.'
            ),
        )
        _ = adopt.add_argument(
            '--from-slot',
            action='store_true',
            help='read it from its workstation slot instead of standard input',
        )
        recover = escrow_verbs.add_parser(
            'recover',
            help='open the escrowed secret with the kit',
            description=(
                "Decrypt one generation of this row with the kit's recovery key. A row that has a "
                'workstation slot is written straight into it, as a `0600` file this command owns rather '
                'than whatever a shell redirect would have created. Anything else is printed -- and '
                'printing is refused when the terminal is the destination, because a secret in the '
                'scrollback is a secret in the next screen share.'
            ),
        )
        _ = recover.add_argument(
            '--generation', type=int, default=None, metavar='<n>', help='which generation to open (default: the newest)'
        )
        _ = recover.add_argument('--stdout', action='store_true', help='print it instead of writing the slot it has')

    return parser


def _layers(held: dict[str, str | None]) -> str:
    """One root's line in `root ls`: where its fields are, never what they are.

    A root whose fields all come from the same layer is one phrase, because
    that is the ordinary case and a field-by-field listing would bury it. The
    mixed case is worth spelling out — a field answered by the environment
    while its siblings sit in the store is usually a shell that will not be
    there next time.
    """
    missing = [name for name, layer in held.items() if layer is None]
    if missing:
        return 'missing: ' + ', '.join(missing)
    layers = {layer for layer in held.values() if layer is not None}
    if len(layers) == 1:
        return f'in {layers.pop()}'
    return ', '.join(f'{name} in {layer}' for name, layer in held.items() if layer is not None)


def _root(args: argparse.Namespace) -> int:
    """The account-root commands, which need no kit and open no database."""
    if args.action == 'ls':
        for account in masters.ROOTS.values():
            print(f'{account.member}: {_layers(masters.stored(account))}')
        return 0
    account = masters.ROOTS[args.member]
    if args.action == 'remember':
        log.info('remembered %s for %s', ', '.join(masters.remember(account, input)), account.member)
    else:
        masters.forget(account)
    return 0


def _kit(args: argparse.Namespace) -> KdbxStore:
    """The seed kit, created on the spot if `kit bootstrap` is starting from none.

    Every other command needs it to exist already: creating one by accident,
    because a path was mistyped, would look like an empty kit rather than a
    missing one.
    """
    path = args.kdbx or default_path()
    if (args.subject, args.member) == ('kit', 'bootstrap') and not path.exists():
        log.info('no kit at %s; creating it', path)
        return KdbxStore.create(path, getpass.getpass(f'new master password for {path.name}: '))
    return KdbxStore.from_env(args.kdbx)


def _stack(args: argparse.Namespace, store: KdbxStore, name: str) -> pulumi_config.Stack:
    """The config slot a derived row is pushed into.

    Opened with the same two variables a `pulumi` run needs, recovered with the
    kit that is already open rather than expected in the environment: one
    command is one credential delivered, not a shell that has to be prepared
    first.

    The stack is named by the caller rather than read off `args`, because only
    one row has a stack to choose: the zones token is scoped to zones and can
    be delivered anywhere, while a row whose credential is named after its
    consumer can only be delivered to that consumer (`derived`).
    """
    return pulumi_config.Stack(
        name=name,
        directory=pulumi_config.project_dir(),
        env=lifecycle.environment(store, args.bundle_dir),
    )


def _slots(args: argparse.Namespace, store: KdbxStore, registry: escrow.Registry) -> slots.Context:
    """What `derived sync` may reach for, with everything slow left unopened.

    The token is fetched up front because every push needs it and the chain
    that finds it may have to ask (`masters.py`); the kit's escrow and the state
    backend are passed as openers, so pushing the one typed-in row asks for
    neither and a row recovered from escrow never reaches for a backend.
    """
    return slots.Context(
        forge=github_secrets.Forge(token=lifecycle.root('github', input)['token']),
        open_vault=lambda: escrow.Vault.open(store, registry),
        open_environment=lambda: lifecycle.environment(store, args.bundle_dir, registry),
    )


def _check(registry: escrow.Registry) -> int:
    """`derived check`, which is the one command that opens nothing.

    Every problem is printed rather than the first one: a registry is checked
    to learn what is wrong with it, and stopping at the first missing label
    would turn one look into several.
    """
    problems = escrow.check(registry)
    for problem in problems:
        log.error('%s', problem)
    if not problems:
        log.info('escrow: %d label(s) present, every ciphertext an age file', len(escrow.register()))
    return 1 if problems else 0


def _imported(args: argparse.Namespace) -> str:
    """The value `derived <row> import` is to escrow: a slot, or standard input.

    Standard input rather than an argument so the value is never in an argv
    another process can read; the slot is there because the passphrase is
    already sitting in one on the workstation that is doing the migration.

    Empty is refused here, where the source is still known, so the error can
    name it: a command substitution or a pipe whose producer failed hands this
    an empty string, and the traceback that says why has already scrolled past
    by the time the import logs what it escrowed.
    """
    if args.from_slot:
        slot = escrow.SLOTS.get(args.label)
        if slot is None:
            raise EscrowError(f'{args.label} has no workstation slot; pipe the value in instead')
        path = slot()
        value = path.read_text().strip()
        if not value:
            raise EscrowError(f'{path} is empty, so there is nothing to escrow as {args.label}')
        return value
    if sys.stdin.isatty():
        log.info('reading the value for %s from standard input; end it with ctrl-d', args.label)
    value = sys.stdin.read().strip()
    if not value:
        raise EscrowError(
            f'standard input was empty, so there is nothing to escrow as {args.label}; '
            'a producer that failed writes no output, so run it on its own and check what it prints '
            'before piping it in again'
        )
    return value


def _pushed(value: str, *, label: str) -> str:
    """Put a freshly generated secret in the workstation slot its row names.

    Only the passphrase has one; the rest reach their consumers through a
    provisioning run or a seal. A label with no slot is not an error — it is
    the ordinary case.
    """
    slot = escrow.SLOTS.get(label)
    if slot is not None:
        _ = workstation.write(slot(), value)
    return value


def _recovered(args: argparse.Namespace, vault: escrow.Vault) -> int:
    """Put one recovered secret where the operator asked for it.

    A label with a workstation slot goes there, so the ordinary path writes a
    `0600` file the command owns rather than something a shell redirect
    created with whatever umask was in force. Everything else is printed, and
    printing is refused when stdout is the terminal: a secret in the
    scrollback is a secret in the next screen-share.
    """
    value = vault.recover(args.label, args.generation)
    slot = escrow.SLOTS.get(args.label)
    if slot is not None and not args.stdout:
        _ = workstation.write(slot(), value)
        log.info('mise.toml reads it from there on every pulumi run')
        return 0
    if sys.stdout.isatty():
        log.error('this prints a secret; pipe it somewhere')
        return 1
    print(value)
    return 0


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format='%(levelname)s: %(message)s')
    args = build_parser().parse_args(argv)

    try:
        if args.subject == 'root':
            return _root(args)
        # Ordered before the registry and the kit, like `derived check` below:
        # the map is a checked-in file, so reading it is something a clone can
        # do with no credential of any kind on the machine.
        if (args.subject, args.member) == ('derived', 'ls'):
            for line in slots.describe():
                print(line)
            return 0
        registry = escrow.Registry.open(args.escrow)
        # Ordered before the kit deliberately: the point of `check` is that a
        # clone with no offline database can still say whether the registry is
        # whole.
        if (args.subject, args.member) == ('derived', 'check'):
            return _check(registry)
        store = _kit(args)

        match (args.subject, args.member, args.action):
            case ('kit', 'ls', _):
                for entry in store.entries(args.group):
                    print(entry)
            case ('kit', 'show', _):
                for name, value in store.describe(args.entry).items():
                    print(f'{name}: {value}')
            case ('kit', 'password', 'remember'):
                # Prove it opens the database before storing it: a remembered
                # password that does not work is worse than none.
                password = getpass.getpass(f'master password for {store.path.name}: ')
                store.unlock_with(password)
                store.remember(password)
            case ('kit', 'password', 'forget'):
                store.forget()
            case ('kit', 'bootstrap', _):
                created = lifecycle.bootstrap(store, prompt=input, only=args.only, registry=registry)
                log.info('created %s', ', '.join(created) if created else 'nothing; the kit was already complete')
                # The recovery row is the one whose creation leaves something
                # outside the kit: the recipients file, and a registry that now
                # has somewhere to put ciphertexts but none of them yet.
                if 'recovery' in created:
                    log.info('the recovery key is in the kit; commit %s', registry.recipients_file)
                    for label in escrow.missing(registry):
                        log.warning(
                            'nothing escrowed for %s yet: credentials derived %s generate',
                            label,
                            escrow.row_name(label),
                        )
            case ('kit', 'rotate', _):
                # Before the successor exists, not once `rotate` reaches its
                # walk: a `--only` that names no row would otherwise leave a
                # new database file behind with nothing rotated into it.
                lifecycle.require_member(args.only)
                successor = KdbxStore.create(args.into, getpass.getpass(f'master password for {args.into.name}: '))
                rotated = lifecycle.rotate(store, successor, prompt=input, only=args.only, registry=registry)
                log.info('rotated %s into %s', ', '.join(rotated), args.into)
                if 'recovery' in rotated:
                    log.warning('commit %s: every ciphertext now opens with the successor key alone', registry.root)
            case ('kit', 'rewrap', _):
                _ = escrow.rewrap(registry, identities=[store.get(escrow.RECOVERY_ENTRY)])
            # One row at a time, through the same dispatch `kit bootstrap`
            # walks: a single-row repair and a whole-kit fill must not be able
            # to write a row two different ways.
            case ('seed', member, 'create') if member in entries.SEEDS:
                lifecycle.create_seed(
                    entries.SEEDS[member], kit=store, prompt=input, entry=args.entry, registry=registry
                )
            case ('seed', 'oci', 'rotate'):
                _ = oci_iam.rotate_seed(store, seed_entry=args.entry)
            # The one-time repair for a kit written before the OCI row
            # carried its identity domain: reading the tenancy's domains is
            # the account root's call, so this is the only seed command that
            # borrows one.
            case ('seed', 'oci', 'domain'):
                _ = oci_iam.adopt_domain(store, seed_entry=args.entry, root=lifecycle.root('oci', input))
            case ('seed', 'b2', 'rotate'):
                _ = b2.rotate_seed(store, seed_entry=args.entry)
            # The minted rows, one command per row (`_stack` is the slot most
            # of them are pushed into).
            case ('derived', derived.ZONES_ROW, 'mint'):
                _ = derived.cloudflare_zones(store, stack=_stack(args, store, args.stack), seed_entry=args.entry)
            case ('derived', derived.OCI_PHYSICAL_ROW, 'mint'):
                _ = derived.oci_physical(
                    store,
                    stack=_stack(args, store, derived.PHYSICAL_STACK),
                    compartment_id=args.compartment,
                    seed_entry=args.entry,
                )
            # The one row that opens no stack: its consumer is what builds
            # the backend a stack's configuration lives in, so the push is a
            # workstation slot and this command needs no `pulumi` at all.
            case ('derived', derived.OCI_STATE_BACKEND_ROW, 'mint'):
                _ = derived.oci_state_backend(store, compartment_id=args.compartment, seed_entry=args.entry)
            case ('derived', derived.B2_MANAGEMENT_ROW, 'mint'):
                _ = derived.b2_management(
                    store, stack=_stack(args, store, derived.PHYSICAL_STACK), seed_entry=args.entry
                )
            # The device rows: no mint, so the command is the console steps
            # plus the push. Which stack takes it comes from the row rather
            # than from an argument -- the credential authenticates against
            # one device, and one stack talks to that device.
            case ('derived', member, 'record') if member in devices.DEVICES:
                device = devices.DEVICES[member]
                _ = devices.deliver(
                    device,
                    stack=_stack(args, store, device.stack),
                    given={field.name: getattr(args, field.dest) for field in device.fields},
                )
            # The escrowed rows. generate -> escrow -> push: the value reaches
            # the slot the map names for it in the same run, and the push lives
            # here rather than in the registry so that opening an escrow never
            # writes to a checkout.
            case ('derived', _, 'generate'):
                _ = _pushed(escrow.generate(registry, args.label), label=args.label)
            case ('derived', _, 'import'):
                _ = escrow.adopt(registry, args.label, _imported(args))
            case ('derived', _, 'recover'):
                return _recovered(args, escrow.Vault.open(store, registry))
            # The slot map's sink: one row at a time or every row whose value
            # lives elsewhere, each resolved, pushed and verified in the same
            # run.
            case ('derived', _, 'sync'):
                filled = slots.sync(_slots(args, store, registry), only=args.only)
                log.info('pushed %s', ', '.join(filled) if filled else 'nothing')
            case ('seed', member, action) if member in entries.SEEDS:
                raise KdbxError(f'`seed {member} {action}` is in the register (§2) but not yet implemented')
            case _:  # pragma: no cover - argparse rejects everything else
                raise ValueError(f'unhandled command {args.subject}')
    except (KdbxError, CredentialRejected, SlotRefused, EscrowError, AgeError) as exc:
        log.error('%s', exc)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
