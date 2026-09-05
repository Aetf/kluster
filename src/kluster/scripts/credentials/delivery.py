"""A minted credential and the retirement it still owes.

The contract is here. The property it keeps belongs to the register --
`docs/credentials.md` §4, under the shape every minting subcommand has -- and
why a platform's retirement runs as the credential it does belongs to that
platform's module, which is what builds the closure.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Delivery[T]:
    """A credential live at its provider, with what it supersedes not yet retired.

    Both fields are private, so a row that reaches for the credential directly
    is a type error rather than a working shortcut. That is the half this type
    enforces. What is left is the callables it hands the value to -- `deliver`'s
    `push` and `about`'s `describe` -- and of those only `deliver` pushes before
    it retires: a `describe` that keeps the value instead of returning a
    description of it defeats the order exactly as a public field would. Python
    cannot constrain what a caller's function does with its argument, so that
    much stays a convention, and it is the one to look for in review.

    The retirement is a closure the mint built rather than a call the caller
    makes, because which credential may run it is the mint's knowledge.
    """

    _credential: T
    _retire: Callable[[], None]

    @staticmethod
    def of[U](credential: U, retire: Callable[[], None]) -> Delivery[U]:
        """What a mint returns: what it created, and how to retire what that replaces.

        A static method with a parameter of its own rather than a classmethod,
        because a classmethod cannot solve the class's parameter from what it
        is handed and would answer `Delivery[Unknown]` at every mint.
        """
        return Delivery(_credential=credential, _retire=retire)

    def about[U](self, describe: Callable[[T], U]) -> Delivery[U]:
        """The same pending retirement, around a fuller description of the same credential.

        For a mint layered on another: the outer one knows things the inner
        call could not report, and neither may take the value out to say so.
        """
        return Delivery(_credential=describe(self._credential), _retire=self._retire)

    def deliver[R](self, push: Callable[[T], R]) -> tuple[T, R]:
        """Push the credential into its slot, and only then retire what it supersedes.

        Answers with the credential and with whatever `push` returned, because
        a caller may need either and this is where both become reachable.
        """
        pushed = push(self._credential)
        self._retire()
        return self._credential, pushed
