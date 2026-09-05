"""The stateless configured provider: the credential is the provider's own.

A dynamic provider is pickled into every resource it manages, so an attribute
carrying a credential would be a copy of that credential on each of them and a
rotation would rewrite all of them. The shape that avoids it is the one every
custom provider here takes (framework/pulumi.md §5.2, rfc-002 §7.4), and this
module is that shape with the parts that differ left abstract:

-   **Nothing is state.** Attributes are unset where the program builds the
    provider and `__getstate__` returns an empty bag, so what lands in state is
    a module name and a class name -- inert, identical on every resource, and
    unchanged by a rotation.
-   **The credential is read in `configure`**, which runs inside the
    resource-provider process, once, before any operation, and receives the
    stack's configuration project-namespaced and with secrets already
    decrypted (rfc-002 §7.5 E2). The process inherits the environment too, so
    exclusivity is this repository's rule rather than the runtime's: a
    credential that only opens the provider's own session lives in stack
    configuration -- unless the credential's own design puts it in the
    environment instead, which is the store rule in style/pulumi.md -- and
    nowhere else: not on a resource, not in a pickle, not in any component's
    signature. Only the store moves. Either way the value is read here, out of
    the process's own configuration or its own environment and by no program,
    and that is what keeps it out of the pickle.
-   **`check` stamps what the pickle no longer shows.** With an inert pickle
    nothing would render a rotation or a change to the provider's own code as a
    diff, so `check` adds two properties no caller declared: `session`, the
    endpoint and a short digest of the credential, and `provider_version`, the
    provider module's own version constant. They are inputs rather than
    outputs because the engine renders its comparison against the checked
    inputs (rfc-002 §7.5 E4, E8).

A subclass supplies four things: which configuration keys hold the credential,
what the fingerprint is taken over, how a property bag reads as an endpoint,
and its own version. Two obligations come with them. **The version is bumped by
hand when an operation's behavior changes** -- a provider class is pickled by
reference, so editing the body of `create` changes not one byte of state and
produces no diff at all (rfc-002 §7.5 E1). And **an update tells a moved stamp
from a changed input**: the stamps move without the far side changing, so an
update that acted on one would rewrite every resource the provider manages on
every rotation, which is the opposite of what the stamps are for.
"""

from __future__ import annotations

import abc
import hashlib
from collections.abc import Mapping
from typing import Any

import pulumi.dynamic as dynamic
from pulumi.runtime import rpc

__all__ = (
    'FINGERPRINT_LENGTH',
    'PROVIDER_VERSION',
    'SESSION',
    'STAMPS',
    'ConfiguredProvider',
    'declared_change',
    'fingerprint',
    'has_unknowns',
    'is_unknown',
)

#: Which door a resource was last written through: the endpoint, and a short
#: digest of the credential that opened it.
SESSION = 'session'

#: The provider module's version, as every resource it manages records it.
PROVIDER_VERSION = 'provider_version'

#: What `check` stamps on a resource that no caller declared.
STAMPS = (SESSION, PROVIDER_VERSION)

#: How much of the credential's digest a resource carries. Enough to tell two
#: credentials apart in a preview, and far too little to be one.
FINGERPRINT_LENGTH = 12


def fingerprint(credential: str) -> str:
    """A credential as a resource may carry it: short, stable, and not the credential."""
    return hashlib.sha256(credential.encode()).hexdigest()[:FINGERPRINT_LENGTH]


def declared_change(olds: Mapping[str, Any], news: Mapping[str, Any], keys: tuple[str, ...]) -> bool:
    """Whether any of `keys` differs between the two bags `diff` was handed.

    Over an explicit list of keys, because the two bags are not symmetrical:
    `olds` is the stored *output* bag while `news` is the checked *input* bag
    (rfc-002 §7.5 E7), so a provider comparing them wholesale sees every
    create-time output as a difference and reports a change on every run.
    """
    return any(olds.get(key) != news.get(key) for key in keys)


def is_unknown(value: Any) -> bool:
    """Whether a property is still a preview placeholder."""
    return isinstance(value, str) and value == rpc.UNKNOWN


def has_unknowns(props: Mapping[str, Any]) -> bool:
    """Whether a property bag still holds preview placeholders.

    During a preview an input may be another resource's unresolved output.
    There is nothing to compare it against and no point reaching the far side
    to try, so the diff answers "unknown" and the engine plans on that basis --
    before any comparison, because a value nobody knows yet cannot have been
    shown to differ and so is never a reason to plan a replacement.
    """
    return any(is_unknown(value) for value in props.values())


class ConfiguredProvider(dynamic.ResourceProvider, abc.ABC):
    """A provider whose connection state is configuration rather than state.

    **The credential is not an attribute until `configure` has run**, which is
    the module docstring's first bullet seen from the inside: the plugin
    deserializes and configures the provider before any operation reaches it
    (rfc-002 §7.5 E2, E3), so an operation may read it. Giving it a default
    would not make anything safer -- it would turn a provider that was never
    configured into one that dials with the wrong credential.
    """

    def configure(self, req: dynamic.ConfigureRequest) -> None:
        """Take the session's credential from the stack's configuration."""
        self._read_credential(req.config)

    def __getstate__(self) -> dict[str, Any]:
        """Nothing at all -- see the class docstring."""
        return {}

    @abc.abstractmethod
    def _read_credential(self, config: dynamic.Config) -> None:
        """Set the attributes an operation dials with, from the stack's configuration.

        A key the configuration lacks raises here, naming the key: a half-filled
        configuration stops the run rather than the session.
        """

    @abc.abstractmethod
    def _credential(self) -> str:
        """What the session fingerprint is taken over.

        The whole of the credential, so that rotating any part of it is a diff.
        """

    @abc.abstractmethod
    def _endpoint(self, props: Mapping[str, Any]) -> str:
        """Where a property bag says the session goes, as one legible string."""

    @abc.abstractmethod
    def _version(self) -> str:
        """The provider module's own version constant.

        A hook rather than a class attribute so the constant stays the module's
        and is looked up when it is stamped: what a subclass returns here is the
        line an author edits to say that an operation's behavior changed.
        """

    def _stamp(self, news: Mapping[str, Any], failures: list[dynamic.CheckFailure]) -> dynamic.CheckResult:
        """The checked inputs, plus the two stamps of the module docstring's third bullet.

        A preview may hand this an endpoint that is still a placeholder, which
        the rendered session string then carries. Nothing reads it: the raw
        property is in the bag too, so `has_unknowns` answers first and no
        comparison of the stamps happens.
        """
        return dynamic.CheckResult({**news, SESSION: self._session(news), PROVIDER_VERSION: self._version()}, failures)

    def _session(self, props: Mapping[str, Any]) -> str:
        """The door this resource is written through: the endpoint, and the credential.

        The digest is stored and previewed in the clear, deliberately. A property
        a provider synthesizes carries no secret marking however secret the
        configuration behind it (rfc-002 §7.5 E10), and this one is meant to be
        read: a truncated digest of a credential is not the credential, and a
        redacted value would make illegible the diff this property exists to
        render.
        """
        return f'{self._endpoint(props)}#{fingerprint(self._credential())}'
