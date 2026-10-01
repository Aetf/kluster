"""The hooks around the appliance's replacement: the permission, the dump, and the restore (rfc-006 §4.3).

The engine replaces the box; these are what it runs around that, in the
program's own process:

-   **`permit`**, before the instance is created, and **`dump`**, before it is
    deleted, both refuse unless the run carries the replacement permission
    (`kluster.lib.state_backend.permission`). That is the gate the engine
    enforces whatever started the run: a step that creates, replaces or
    deletes the box fails with the box untouched. Both, because under
    `delete_before_replace` the old box is deleted before the new one is
    created, so the create hook alone would refuse with the old box gone.
-   **`dump`** then takes the dump the box is about to lose, through the
    reserved address, which still points at the old box: the encrypted file
    goes where `state-backend dump` writes one, and the plaintext stays in a
    directory private to this process for `restore`. A dump that fails
    raises, and a raising before hook leaves the delete unexecuted, so the old
    box keeps serving.
-   **`restore`**, after the readiness resource is created, which is once the
    new box answers on the reserved address. It restores this run's plaintext
    and asks `pulumi` what the backend then serves. A run that took no dump of
    its own reads the backend instead: one that holds a stack is a box this
    run did not replace (the readiness resource can be re-created without a
    new box), and one that holds none is a box owed a restore, or a site
    before its first `stack init`, which the hook names and refuses.

The hooks connect with the `operator` client bundle, which authenticates to
the estate's backend rather than to any provider of this stack: the stack
program reads its slot's directory and hands it down, and the hooks hold it
here (credentials.md §1, rule 6).
"""

from __future__ import annotations

import logging
import tempfile
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

import pulumi

from kluster.lib.state_backend import permission, settings, state
from putils import background

log = logging.getLogger(__name__)

__all__ = ('HookRefused', 'Replacement')


class HookRefused(RuntimeError):
    """A hook stopped the step it runs before, or failed the one it runs after."""


def _refuse_without_permission(step: str, granted: Callable[[], bool]) -> None:
    if not granted():
        raise HookRefused(
            f'this run would {step} the state-backend box, and it does not carry the permission to: '
            f'`{permission.REMEDY}` is the run that does ({permission.ENV} is set by the driver for it)'
        )


@dataclass
class Replacement:
    """The three hooks, and what they share: the bundle they connect with, the dump's recipients and where it goes.

    One instance per program run. The plaintext a dump leaves is this
    instance's: `restore` reads it, and the directory holding it is removed
    when the instance is, which is when the program's process ends.
    """

    #: The `operator` client bundle's directory (`state.connection`).
    bundle_dir: Path
    #: The recipients every dump is encrypted to: the box's own.
    recipients: tuple[str, ...]
    #: Where the encrypted dump is written: where `state-backend dump` writes one.
    dump_directory: Path
    #: Whether this run carries the permission. Read when a hook runs, never
    #: before.
    granted: Callable[[], bool] = permission.granted
    _plaintext: tempfile.TemporaryDirectory[str] | None = field(default=None, init=False, repr=False)
    _dumped: Path | None = field(default=None, init=False, repr=False)

    @property
    def archive(self) -> Path | None:
        """The plaintext of the dump this run took, if it took one."""
        return None if self._plaintext is None else Path(self._plaintext.name) / 'state.dump'

    def permit_now(self) -> None:
        """`permit`'s work: refuse a create without the permission."""
        _refuse_without_permission('create', self.granted)

    def dump_now(self) -> Path:
        """`dump`'s work: refuse without the permission, then dump. Returns the encrypted dump."""
        _refuse_without_permission('delete', self.granted)
        destination = self.dump_directory / state.dump_name()
        plaintext = tempfile.TemporaryDirectory(prefix=f'{settings.NAME}-')
        log.info('dumping the box about to be deleted, into %s', destination)
        try:
            state.take_dump(
                destination,
                bundle_dir=self.bundle_dir,
                recipients=self.recipients,
                archive=Path(plaintext.name) / 'state.dump',
            )
        except BaseException:
            plaintext.cleanup()
            raise
        self._plaintext = plaintext
        self._dumped = destination
        return destination

    def restore_now(self) -> list[str]:
        """`restore`'s work: restore this run's dump, or read a backend this run did not dump. Returns its stacks."""
        target = state.connection(self.bundle_dir)
        archive = self.archive
        if archive is not None:
            # The guard `state-backend restore` runs: a restore replaces
            # whatever the backend holds, so one that serves a stack is never
            # restored over.
            if serving := state.served(target):
                raise HookRefused(
                    f'{state.endpoint(target.url)} already serves {len(serving)} stack(s), so the dump this run '
                    f'took is not restored over them; it is in {self._dumped}'
                )
            log.info('restoring the dump this run took into the new box')
            state.pg_restore(target, archive)
        held = state.stacks(target)
        if held:
            log.info('the backend serves %d stack(s)', len(held))
            return held
        if archive is not None:
            raise HookRefused(
                f'the dump this run took went into {state.endpoint(target.url)}, and `pulumi stack ls` lists no '
                'stack there: the encrypted dump beside it is the one to restore with `state-backend restore <file>`'
            )
        raise HookRefused(
            f'{state.endpoint(target.url)} serves no stack, and this run took no dump to restore into it: '
            '`state-backend restore <file>` restores the dump the box was replaced from, or the newest nightly '
            'one; a site with no state yet needs only its first `pulumi stack init` against it'
        )

    async def permit(self, _args: pulumi.ResourceHookArgs) -> None:
        await background(self.permit_now)()

    async def dump(self, _args: pulumi.ResourceHookArgs) -> None:
        _ = await background(self.dump_now)()

    async def restore(self, _args: pulumi.ResourceHookArgs) -> None:
        _ = await background(self.restore_now)()
