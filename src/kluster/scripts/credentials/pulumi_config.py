"""The Pulumi config-secret slot: a value delivered into a stack's committed configuration.

One of the register's storage channels (docs/credentials.md §1 rule 6), and the
narrower of the two Pulumi ones: `Pulumi.<stack>.yaml` is committed, so its
ciphertext is public the moment the repository is, and only credentials a
program needs *before* it can run belong here. What lands is ciphertext under
the stack's own passphrase — the Pulumi stack passphrase, `physical`'s
passphrase, or, for an operator stack, the operator passphrase — each escrowed
to the kit (§2.2), so a slot written here opens from the kit, or from a copy
of the passphrase recovered from it.

Driven through the `pulumi` CLI rather than the automation API because that is
what writes the file the operator then commits, and because the CLI is already
the pinned tool every other Pulumi step in this repository uses (`mise.toml`).
A secret value is handed over on standard input: an argument would put the
credential in the process table of a shared machine.
"""

from __future__ import annotations

import json
import logging
import re
import shutil
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast

from kluster.conventions import identity
from kluster.lib import pulumi_cli, stack_environment
from kluster.lib.pulumi_cli import Runner

log = logging.getLogger(__name__)


class SlotRefused(RuntimeError):
    """A slot would not take the value — the push failed, so nothing consumed it."""


class PassphraseMissing(SlotRefused):
    """A stack encrypted apart, on a machine holding no passphrase to open it with.

    Its own type because it is the one refusal here that is about the *machine*
    rather than about the stack's contents, and a caller that dresses a refusal
    up in its own words has to let this one through unchanged: "the credential
    is not in the config" and "this machine cannot read that config at all" send
    an operator to different places.
    """


def run_pulumi(args: Sequence[str], *, cwd: Path, env: Mapping[str, str], stdin: str | None) -> str:
    """Run one `pulumi` command (`pulumi_cli.run_pulumi`), a refusal refused as a slot.

    The boundary between the runner, which `kluster.lib` holds for every
    caller, and this package: a `pulumi` run that failed here is a slot that
    would not take the value, which is what every caller in this package
    catches.
    """
    try:
        return pulumi_cli.run_pulumi(args, cwd=cwd, env=env, stdin=stdin)
    except pulumi_cli.PulumiRefused as exc:
        raise SlotRefused(str(exc)) from exc


def project_dir() -> Path:
    """The checkout holding `Pulumi.yaml` (`pulumi_cli.project_dir`), a refusal refused as a slot."""
    try:
        return pulumi_cli.project_dir()
    except pulumi_cli.PulumiRefused as exc:
        raise SlotRefused(str(exc)) from exc


#: The two variables a `pulumi` run in this repository is given. Named,
#: because both the record below and the slot map spell them; the mapping from
#: a stack to their values is `kluster.lib.stack_environment`'s.
BACKEND_URL_ENV = stack_environment.BACKEND_URL_ENV
PASSPHRASE_ENV = stack_environment.PASSPHRASE_ENV

#: Every stack that is **not** on the stack passphrase, and the register row
#: (§3) the one it *is* on -- the operator passphrase -- comes from. A census
#: rather than a consequence of whatever a run happened to find: a stack named
#: here on a machine holding no operator passphrase is refused *here*, by
#: name, instead of being run under the stack passphrase — which `pulumi` would answer with `error: incorrect
#: passphrase`, a refusal that names neither the stack nor the fix and arrives
#: at the far end of whatever command was in progress.
#:
#: The value is the row rather than a sentence about it, so the refusal below
#: composes the command and a test can follow the row into the slot map. That
#: is what holds this honest: a stack cannot be taken off the stack passphrase
#: without a register row that generates and escrows one.
#:
#: Read off the operator-stack census (`conventions.identity.OPERATOR_STACKS`)
#: rather than written beside it: what sets a stack apart is that no CI job
#: runs it, and every stack of that kind is under the operator passphrase, the
#: one no CI Environment holds (framework/pulumi.md §3.3).
APART: Mapping[str, str] = dict.fromkeys(identity.OPERATOR_STACKS, stack_environment.OPERATOR_PASSPHRASE_ROW)

#: The stack CI runs that is **not** on the stack passphrase either, and the
#: register row its own passphrase comes from. `physical`'s provider
#: credentials can root the gateway, and the stack passphrase is in every
#: Environment a pull request can reach, so `physical` is encrypted under a
#: passphrase that reaches only `physical-plan` and `physical`, which take
#: protected branches only (rfc-005 §5.1). Asked for here, by name, for the
#: reason `APART` is: a run under the stack passphrase would end in `error:
#: incorrect passphrase`, which names neither the stack nor the fix.
PHYSICAL = stack_environment.PHYSICAL_STACK
PHYSICAL_ROW = stack_environment.PHYSICAL_PASSPHRASE_ROW

#: Every stack under the stack passphrase, in the census's order: each stack
#: of the project that is neither apart (`APART`) nor `physical`. Derived
#: rather than listed, so a stack added to `conventions.identity.STACK_NAMES`
#: is one `credentials derived pulumi-passphrase re-encrypt` moves without a
#: second edit, and the test holding it asks `BackendEnvironment.variables`
#: which stacks a run hands the stack passphrase to.
ON_STACK_PASSPHRASE: tuple[str, ...] = tuple(
    name for name in identity.STACK_NAMES.names() if name not in APART and name != PHYSICAL
)


@dataclass(frozen=True)
class BackendEnvironment:
    """What this machine can tell a `pulumi` run about the state backend.

    A closed shape rather than a mapping, because the key set is closed and
    because a caller reads the URL by name — a workstation without a client
    bundle has no URL, and that is a state to be handled rather than a key
    that happens to be missing from a bag. `variables()` is the one place it
    becomes the environment a process is started with, so an absent half
    is an absent variable rather than an empty one.

    **`variables` takes the stack it is building an environment for**, because
    `PULUMI_CONFIG_PASSPHRASE` is process-global while this installation has
    more than one: the operator stacks' configuration is encrypted under the
    operator passphrase, which reaches no CI Environment and is what confines
    those stacks to the workstation, and `physical`'s under a passphrase of
    its own, which reaches no Environment a pull request can (credentials.md
    §2.2). A caller therefore
    cannot build "the environment" — only the environment for a named stack —
    and `Stack` derives it from its own name so that no call site can pair one
    stack with another's passphrase.
    """

    #: The stack passphrase, which every stack but `physical` and the
    #: operator stacks is given. It does not print; the URL is what a repr is
    #: read for.
    passphrase: str | None = field(default=None, repr=False, compare=False)
    url: str | None = None
    #: Where the operator passphrase is found, for the stacks in `APART` and
    #: no other: the acquisition chain (`stack_environment.operator_passphrase`)
    #: for a run on a workstation. A function rather than a value, so it is
    #: called only when such a stack is asked for and a command that never
    #: points at one never looks, let alone prompts; the caller makes it answer
    #: once per command. None looks nowhere, and such a stack is refused.
    operator: Callable[[], str] | None = field(default=None, compare=False)
    #: Where `physical`'s passphrase is found, for that stack and no other: the
    #: kit's escrow, for a `credentials` run. A function for the reason
    #: `operator` is one -- a command that never points at `physical` never
    #: opens the label, and a kit with nothing filed under it refuses only the
    #: commands that do. None looks nowhere, and `physical` is refused.
    physical: Callable[[], str] | None = field(default=None, compare=False)

    def variables(self, stack: str, *, checkout: Path | None = None) -> dict[str, str]:
        """The variables a `pulumi` run against `stack` is started with.

        The backend is the stack's own (`stack_environment.backend_variables`):
        the URL held here, or, for a stack whose state is committed, the
        `checkpoints/` directory of `checkout` — the checkout holding
        `Pulumi.yaml` unless one is named.
        """
        row = APART.get(stack)
        if row is not None:
            chosen = self._operator(stack, row)
        elif stack == PHYSICAL:
            chosen = self._physical(stack)
        else:
            chosen = self.passphrase
        values: dict[str, str] = {}
        if chosen is not None:
            values[PASSPHRASE_ENV] = chosen
        if checkout is None and stack_environment.home(stack) is identity.StateHome.COMMITTED:
            checkout = project_dir()
        return values | stack_environment.backend_variables(stack, checkout=checkout, estate_url=self.url)

    def _operator(self, stack: str, row: str) -> str:
        """The operator passphrase for `stack`, from `operator`; `PassphraseMissing` where it finds none.

        The refusal carries the chain's own, which says where it looked and
        names the escrow as the recovery path, since that is what fills the
        chain on a machine that holds the kit.
        """
        reason = (
            f'nothing here looks for it; `credentials derived {row} recover` puts it where the acquisition '
            'chain finds it, on a machine that holds the kit'
        )
        if self.operator is not None:
            try:
                return self.operator()
            except stack_environment.EnvironmentRefused as exc:
                reason = str(exc)
        raise PassphraseMissing(
            f"the {stack} stack's configuration is encrypted under the operator passphrase, and this "
            f'machine cannot give it: {reason}. `credentials derived {row} generate` makes the first one. '
            f'The stack passphrase the other stacks share is deliberately not used here — every '
            f'Environment a pull request can reach holds that one, and the operator stacks are the ones '
            f'nothing in CI may read (framework/github.md §1).'
        )

    def _physical(self, stack: str) -> str:
        """`physical`'s own passphrase, from `physical`; `PassphraseMissing` where it finds none."""
        reason = (
            f'nothing here looks for it: a `credentials` run recovers it with the kit, and `credentials derived '
            f'{PHYSICAL_ROW} generate` files the first'
        )
        if self.physical is not None:
            try:
                return self.physical()
            except stack_environment.EnvironmentRefused as exc:
                reason = str(exc)
        raise PassphraseMissing(
            f"the {stack} stack's configuration is encrypted under a passphrase of its own, and this machine "
            f'cannot give it: {reason}. The stack passphrase the previewed stacks share is deliberately not used '
            f'here — every Environment a pull request can reach holds that one, and {stack} is the stack none of '
            'them may read (framework/github.md §1).'
        )


@dataclass(frozen=True)
class Stack:
    """One stack's committed configuration, as a slot that takes values.

    The environment every invocation runs with is derived **here**, from this
    stack's own name, rather than handed in ready-made. That is the whole
    guard against the trap `BackendEnvironment` describes: a passphrase is
    process-global, so a caller that built the variables itself could hand
    this one another stack's, and a `pulumi` run under the wrong passphrase is
    a class of bug worth making unreachable rather than merely unlikely.

    It would not be a *silent* bug — `encryptionsalt` is a verifier, so
    `pulumi` answers `error: incorrect passphrase` and exits non-zero without
    touching the file — but the refusal names neither the stack nor the fix,
    and a run that fails at the far end of a bring-up is expensive to read.
    """

    name: str
    directory: Path
    #: What this machine can say about the state backend, which the caller
    #: derives from the kit rather than expecting in the ambient environment.
    #: The stack's own passphrase is picked out of it by name below.
    environment: BackendEnvironment = field(default_factory=lambda: BackendEnvironment())
    run: Runner = run_pulumi

    @property
    def env(self) -> Mapping[str, str]:
        """The variables a `pulumi` run against *this* stack is started with."""
        return self.environment.variables(self.name, checkout=self.directory)

    def _pulumi(self, *args: str, stdin: str | None = None) -> str:
        return self.run([*args], cwd=self.directory, env=self.env, stdin=stdin)

    def exists(self) -> bool:
        return self.name in self._stack_names(self._pulumi('stack', 'ls', '--json'))

    @staticmethod
    def _stack_names(printed: str) -> set[str]:
        """The names in `pulumi stack ls --json` output, or a refusal quoting it.

        The boundary for that command. A silent empty answer here reads as "no
        such stack", which makes `ensure` try to create one that exists.
        """
        try:
            listing: object = json.loads(printed or '[]')
        except ValueError as exc:
            raise SlotRefused(f'`pulumi stack ls --json` did not print JSON: {printed[:120]!r}') from exc
        if not isinstance(listing, list):
            raise SlotRefused(f'`pulumi stack ls --json` printed a {type(listing).__name__}, not a list of stacks')
        found: set[str] = set()
        for index, entry in enumerate(cast('list[object]', listing)):
            name = cast('dict[str, object]', entry).get('name') if isinstance(entry, dict) else None
            if not isinstance(name, str) or not name:
                raise SlotRefused(f'`pulumi stack ls --json` entry {index} carries no name, and is {entry!r}')
            found.add(name)
        return found

    def ensure(self) -> None:
        """Create the stack if the backend has none of that name.

        `--no-select` because a credentials run is not a development session:
        which stack the operator had selected is theirs, and a push must not
        change it under them.
        """
        log.info('checking the state backend for the %s stack', self.name)
        if self.exists():
            return
        log.info('no %s stack yet; creating it', self.name)
        _ = self._pulumi('stack', 'init', self.name, '--no-select')

    def get(self, key: str) -> str:
        return self._pulumi('config', 'get', key, '--stack', self.name).strip()

    def holds(self, key: str, value: str) -> bool:
        """Whether `key` already holds `value`, stored as a secret.

        Both halves, because the decrypted value alone cannot tell a secret
        from the same text written in the clear, and a plain copy of a
        credential in a committed file is one to replace rather than to leave
        alone. `config get --json` answers with the value and whether it is
        stored encrypted, and without the newline the plain form prints.

        A key this cannot read -- absent, or refused for any other reason --
        counts as not held, so the caller goes on to write it, and that write
        either fills the slot or refuses with its own reason. Nothing is
        logged: what is compared is a secret.
        """
        try:
            printed = self._pulumi('config', 'get', key, '--json', '--stack', self.name)
        except SlotRefused:
            return False
        try:
            entry: object = json.loads(printed)
        except ValueError:
            return False
        if not isinstance(entry, dict):
            return False
        held = cast('dict[str, object]', entry)
        return held.get('secret') is True and held.get('value') == value

    def outputs(self) -> dict[str, Any]:
        """Every output of the stack's current state, secrets included.

        The other direction of this module: what a *program* generated, rather
        than what one needs to start. `--show-secrets` is what makes reading a
        secret output possible at all — without it the value comes back as the
        literal string `[secret]`, which a caller would deliver as if it were
        the secret.
        """
        raw = self._pulumi('stack', 'output', '--json', '--show-secrets', '--stack', self.name).strip()
        try:
            parsed: object = json.loads(raw or '{}')
        except json.JSONDecodeError as exc:
            # The size and where the parse stopped, never the text: this is a
            # dump whose values are secrets by design, and the refusal is
            # logged. Not `str(exc)` either -- two of the decoder's messages
            # quote the character they stopped on.
            raise SlotRefused(
                f'`pulumi stack output --json` did not print JSON: {len(raw)} characters, '
                f'and parsing stopped at line {exc.lineno} column {exc.colno}'
            ) from exc
        if not isinstance(parsed, dict):
            raise SlotRefused(f'`pulumi stack output --json` printed a {type(parsed).__name__}, not an object')
        return cast('dict[str, Any]', parsed)

    def set(self, key: str, value: str) -> None:
        """Write a non-secret key, in plain text in the committed file, and read it back.

        On standard input, as a secret is, though this value is public: the
        CLI reads an argument that opens with a dash as a flag (`bad flag
        syntax`), and a PEM certificate opens with five. The read-back is the
        proof `set_secret` makes.
        """
        log.info('setting %s on the %s stack', key, self.name)
        _ = self._pulumi('config', 'set', key, '--stack', self.name, stdin=value)
        if self.get(key) != value:
            raise SlotRefused(f'{key} on the {self.name} stack does not read back as what was just written')

    def set_plain_at(self, path: str, value: str) -> None:
        """Write `value` in the clear at a path inside a structured key, and read it back as written and as plain.

        The channel of a sealed value (`conventions.sealed`): ciphertext that
        needs no stack encryption, committed in the clear. `--plaintext`
        because the CLI refuses a value that looks like a secret without
        either it or `--secret`, and a sealed value always does. The read-back
        asks for the stored form as well as the value, because a value written
        encrypted reads back equal and is the wrong channel: the program reads
        the key without decrypting it, and the file would carry a second
        layer of encryption for no reader.
        """
        log.info('writing %s into the %s stack config in the clear', path, self.name)
        _ = self._pulumi('config', 'set', '--path', '--plaintext', path, '--stack', self.name, stdin=value)
        printed = self._pulumi('config', 'get', '--path', path, '--json', '--stack', self.name)
        try:
            entry: object = json.loads(printed)
        except ValueError as exc:
            raise SlotRefused(f'`pulumi config get --path {path} --json` did not print JSON') from exc
        held = cast('dict[str, object]', entry) if isinstance(entry, dict) else {}
        if held.get('secret') is not False:
            raise SlotRefused(f'{path} on the {self.name} stack is not stored in the clear after being written so')
        if held.get('value') != value:
            raise SlotRefused(f'{path} on the {self.name} stack does not read back as what was just written')

    def set_secret(self, key: str, value: str) -> None:
        """Write a secret key, and read it back to prove the slot holds it.

        The read-back is what makes the push verifiable at all: the file gains
        ciphertext either way, and the only thing that distinguishes a delivered
        credential from a corrupted one is decrypting it again.
        """
        log.info('encrypting %s into the %s stack config', key, self.name)
        _ = self._pulumi('config', 'set', key, '--secret', '--stack', self.name, stdin=value)
        if self.get(key) != value:
            raise SlotRefused(f'{key} on the {self.name} stack does not decrypt to what was just written')

    def fill(self, *, secret: Mapping[str, str], plain: Mapping[str, str], holds: str) -> None:
        """Fill this stack's committed configuration, and say what has to be committed.

        Every register row delivered to a stack closes the same way, and the
        closing is the half an operator acts on: the stack has to exist before
        it has a configuration to set, the secret half goes in encrypted and the
        plain half readable, and the run ends by naming the file that publishes
        the slot. A push that stopped at `pulumi config set` would leave the
        credential live in the provider and invisible to everyone else's
        checkout.
        """
        self.ensure()
        for key, value in secret.items():
            self.set_secret(key, value)
        for key, value in plain.items():
            self.set(key, value)
        log.info('the %s stack holds %s; commit Pulumi.%s.yaml to publish the slot', self.name, holds, self.name)

    def re_encrypt(self, *, former: Sequence[str]) -> bool:
        """Move this stack's configuration and its state onto its own passphrase, from whichever of `former` opens it.

        `former` is every passphrase the stack may be under now, in the order
        they are tried. Returns whether anything moved: False for a stack
        already under its own. **Two kinds of run in this module start under a
        passphrase that is not the stack's own, and both are here**: the
        probes of each former passphrase, and the move. A re-encryption has to
        open what it moves: `pulumi stack change-secrets-provider passphrase`
        decrypts the configuration and the state with the passphrase in the
        environment and reads the one it moves them onto from standard input,
        where a run with no terminal finds it, so neither is in an argument.
        It rewrites the stack file first, then imports the state again under
        the new salt.

        **Which passphrase the stack is under is read off its salt**
        (`_salt_verifies`), not off a decryption: a configuration holding no
        secret decrypts under anything, so a decryption would call such a
        stack under every passphrase in `former` and move it again on every
        run.

        **Moved means that both decrypt under the stack's own passphrase**:
        the configuration (`_opens_under`) and the state (`_state_opens_under`).
        That is the test for a stack already there, and the check after the
        move, which also asks that the stack file carry a new salt: every
        move mints one, and a configuration and a state holding no secret
        decrypt under anything, so without it a run that wrote nothing would
        pass. Equal salts are not asked for: a run repeated over a finished
        move whose stack file was lost rewrites the file under a fresh salt
        and then fails on the state, already moved, and the stack it leaves
        opens whole under its own passphrase.
        """
        own = self.env[PASSPHRASE_ENV]
        if not self.exists():
            raise SlotRefused(self._absent())
        log.info('checking which passphrase the %s stack is under', self.name)
        current = next((passphrase for passphrase in former if self._salt_verifies(passphrase)), None)
        if current is None:
            if not self._salt_verifies(own) or not self._config_opens_under(own):
                raise SlotRefused(
                    f'the {self.name} stack opens under none of the passphrases it may be moved from, nor under '
                    'its own, so nothing here can re-encrypt it'
                )
            if not self._state_opens_under(own):
                raise SlotRefused(
                    f"the {self.name} stack's configuration is under its own passphrase and its state is not. "
                    f'A re-encryption interrupted between the two leaves that: {self._recovery()}. So does a '
                    f'run from a checkout still under the earlier passphrase that wrote the state back after this '
                    f'one moved it, and the committed file is then the moved one: restore the '
                    f'Pulumi.{self.name}.yaml `main` held before the move (`git -C {self.directory} checkout '
                    f'<that commit> -- Pulumi.{self.name}.yaml`) and run this again'
                )
            log.info('the %s stack is already under its own passphrase; nothing to move', self.name)
            return False
        log.info('re-encrypting the %s stack: its configuration, then its state in the backend', self.name)
        salt = self._salt()
        failure: SlotRefused | None = None
        try:
            _ = self.run(
                ['stack', 'change-secrets-provider', 'passphrase', '--stack', self.name],
                cwd=self.directory,
                env={**self.env, PASSPHRASE_ENV: current},
                stdin=own,
            )
        except SlotRefused as exc:
            failure = exc
        # Every move mints a salt, so an unchanged one is a run that moved
        # nothing, whatever decrypts afterward: a configuration and a state
        # holding no secret decrypt under anything.
        moved = self._salt() != salt
        if moved and self._opens_under(own) and self._state_opens_under(own):
            if failure is not None:
                log.info(
                    'the re-encryption stopped at the state, which opens under its own passphrase (it was already '
                    'there, or holds nothing encrypted): %s',
                    failure,
                )
            log.info(
                'the %s stack is under its own passphrase; commit Pulumi.%s.yaml, whose salt and ciphertexts moved',
                self.name,
                self.name,
            )
            return True
        if failure is not None and not moved:
            raise SlotRefused(
                f'{failure}; Pulumi.{self.name}.yaml was not rewritten, so nothing moved: run this again once '
                'what stopped it is gone'
            ) from failure
        if failure is not None:
            raise SlotRefused(
                f'{failure}; Pulumi.{self.name}.yaml may already be rewritten: {self._recovery()}'
            ) from failure
        raise SlotRefused(f'the {self.name} stack does not read back under its own passphrase after the re-encryption')

    def _absent(self) -> str:
        """The refusal for a stack the backend does not hold, naming what makes it, which a re-run then finds."""
        create = f'`mise x -- pulumi stack init {self.name} --no-select` from the checkout'
        if not (self.directory / f'Pulumi.{self.name}.yaml').exists():
            return (
                f'the state backend holds no {self.name} stack, and nothing here moves past a stack it cannot '
                f'open: create it first with {create}, which makes it under its own passphrase, and run this again'
            )
        return (
            f'the state backend holds no {self.name} stack, though the checkout holds Pulumi.{self.name}.yaml, and '
            f'nothing here moves past a stack it cannot open: create it first with {create}, under the passphrase '
            "that file's salt was made under, and run this again"
        )

    def _recovery(self) -> str:
        """The way forward from a re-encryption that stopped partway, naming the checkout it ran in."""
        return (
            f'restore the committed Pulumi.{self.name}.yaml (`git -C {self.directory} checkout -- '
            f'Pulumi.{self.name}.yaml`) and run this again'
        )

    def _salt(self) -> str | None:
        """The stack file's `encryptionsalt`, a plain line of the file and no secret."""
        found = _SALT.search((self.directory / f'Pulumi.{self.name}.yaml').read_text())
        return found.group(1) if found is not None else None

    def _config_opens_under(self, passphrase: str) -> bool:
        """`_opens_under`, with any refusal other than a wrong passphrase carrying the recovery.

        A run stopped between `change-secrets-provider`'s two saves of the
        stack file leaves a new salt beside ciphertexts under the old one, and
        `pulumi` refuses that file with an error of its own rather than with
        `incorrect passphrase`; the recovery from it is the interrupted run's.
        """
        try:
            return self._opens_under(passphrase)
        except SlotRefused as exc:
            raise SlotRefused(
                f'{exc}; if a re-encryption of the {self.name} stack stopped while it was rewriting the stack file, '
                f'{self._recovery()}'
            ) from exc

    def _salt_verifies(self, passphrase: str) -> bool:
        """Whether this stack file's `encryptionsalt` was made under `passphrase`, whatever the file holds.

        Asked of `pulumi config set --secret`, which checks the salt before it
        encrypts anything, so it answers `incorrect passphrase` for a
        configuration holding no secret too, where `config --show-secrets`,
        having nothing to decrypt, never reads the salt. It writes to a
        scratch copy of the file, never to the stack file; the probe value is
        no secret.
        """
        with tempfile.TemporaryDirectory(prefix='kluster-salt-') as scratch:
            copy = Path(scratch) / f'Pulumi.{self.name}.yaml'
            shutil.copyfile(self.directory / f'Pulumi.{self.name}.yaml', copy)
            return self._decrypts(
                ['config', 'set', _SALT_PROBE, '--secret', '--config-file', str(copy), '--stack', self.name],
                passphrase,
                stdin=_SALT_PROBE,
            )

    def _opens_under(self, passphrase: str) -> bool:
        """Whether this stack's configuration decrypts under `passphrase`.

        Every secret is decrypted and the answer discarded, so what is learned
        is the verdict alone: `pulumi` refuses a passphrase the salt does not
        verify with `incorrect passphrase`, and any other failure is a
        refusal of its own rather than a no.
        """
        return self._decrypts(['config', '--show-secrets', '--json', '--stack', self.name], passphrase)

    def _state_opens_under(self, passphrase: str) -> bool:
        """Whether this stack's state decrypts under `passphrase`, read as `_opens_under` reads the configuration.

        A state holding nothing encrypted opens under anything, which is the
        answer wanted: there is nothing in it to move.
        """
        return self._decrypts(['stack', 'export', '--show-secrets', '--stack', self.name], passphrase)

    def _decrypts(self, args: Sequence[str], passphrase: str, *, stdin: str | None = None) -> bool:
        try:
            _ = self.run(list(args), cwd=self.directory, env={**self.env, PASSPHRASE_ENV: passphrase}, stdin=stdin)
        except SlotRefused as exc:
            if 'incorrect passphrase' in str(exc):
                return False
            raise
        return True


#: The salt line of a stack file, which `pulumi` writes when it creates or
#: re-encrypts the stack.
_SALT = re.compile(r'^encryptionsalt:\s*(\S+)\s*$', re.MULTILINE)

#: The key `Stack._salt_verifies` encrypts into a scratch copy of a stack file,
#: and the value it encrypts: neither is a secret, and neither reaches the
#: stack file.
_SALT_PROBE = 'kluster-salt-probe'


def re_encrypt_on_stack_passphrase(
    environment: BackendEnvironment,
    *,
    former: Sequence[str],
    directory: Path | None = None,
    run: Runner = run_pulumi,
) -> list[str]:
    """Move every stack under the stack passphrase onto the one `environment` holds, from whichever of `former` opens it.

    Each stack in `ON_STACK_PASSPHRASE` is moved by `Stack.re_encrypt`, which
    proves the move took and leaves a stack already under the newest
    generation alone. A stack the run cannot move is refused there, naming its
    recovery, and the stacks after it are not reached: a re-run moves them and
    leaves the ones already moved alone. Returns the stacks that moved, in the
    census's order.
    """
    where = directory if directory is not None else project_dir()
    return [
        name
        for name in ON_STACK_PASSPHRASE
        if Stack(name=name, directory=where, environment=environment, run=run).re_encrypt(former=former)
    ]
