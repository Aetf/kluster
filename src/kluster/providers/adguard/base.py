"""The lifecycle every AdGuard kind shares, and what a kind supplies to it.

`InstanceProvider` is what every kind shares: the login, the stamps, `check`,
`diff` and `delete`. `AdGuardProvider` adds the lifecycle of the kinds that
configure a running instance, every kind but `AdGuardSetup`, whose lifecycle is
its own (`setup`).

A kind declares its object as **sections**: top-level properties, each in the
shape the endpoint that owns it reads and writes. The base turns those into the
dynamic-provider operations:

-   **`check`** refuses what the kind refuses offline, and stamps `session` and
    `provider_version` through `kluster.providers.configured`.
-   **`diff`** calls no instance. A value still unknown answers unknown; a
    changed `instance` is a replacement; otherwise it compares each section in
    the kind's comparable form, `endpoint`, `setup_endpoint` and the stamps.
-   **`read`** classifies the instance (`api.classify`). An instance in first
    run holds no configuration at all, so the resource is reported gone, and
    the run that refreshes re-creates it after the instance's setup. A
    configured one is dialed, and its sections are returned as the outputs and
    as the inputs, so a refresh renders a hand edit as a property diff against
    the program. `instance`, `endpoint`, `setup_endpoint` and the stamps come
    from the stored bag. Any other verdict raises, sending no login.
-   **`create`** writes every section. **`update`** writes only when a section
    differs from the stored outputs; a moved endpoint or stamp alone calls
    nothing and is recorded. Both refuse an instance their setup has not
    configured yet.
-   **`delete`** calls nothing: an instance's settings have no absent state to
    restore, and a replacement deletes after it creates, so a delete that
    emptied anything would undo the create before it.

The id is the instance and the kind, never the endpoint.
"""

from __future__ import annotations

import abc
from collections.abc import Mapping, Sequence
from typing import Any, ClassVar, cast

import pulumi.dynamic as dynamic

from kluster.providers.adguard.api import Api, Verdict, classify
from kluster.providers.configured import STAMPS, ConfiguredProvider, is_unknown

__all__ = (
    'ENDPOINT',
    'INSTANCE',
    'PASSWORD_CONFIG',
    'RECORDED',
    'SETUP_ENDPOINT',
    'USERNAME_CONFIG',
    'VERSION',
    'AdGuardProvider',
    'InstanceProvider',
    'SetupNotRun',
    'gone',
    'missing_or_extra',
    'unknown_anywhere',
)

#: The stack-configuration keys the admin login is read from (`configured`).
#: Both halves, because an admin login is a pair.
USERNAME_CONFIG = 'adguardUsername'
PASSWORD_CONFIG = 'adguardPassword'

#: The caller's name for the instance. It identifies the resource, so a change
#: is a replacement.
INSTANCE = 'instance'

#: Where this run reaches the instance's administration API. A change is an
#: update that writes nothing: the same instance, re-addressed.
ENDPOINT = 'endpoint'

#: Where this run reaches the instance's setup wizard, which answers only in
#: first run. Recorded like `endpoint`: a change writes nothing.
SETUP_ENDPOINT = 'setup_endpoint'

#: This package's version, bumped by hand when an operation's behavior changes
#: (`configured`). One for the package, since the kinds share the lifecycle.
VERSION = '2'

#: The inputs every kind records and never writes.
RECORDED = (INSTANCE, ENDPOINT, SETUP_ENDPOINT, *STAMPS)


class SetupNotRun(RuntimeError):
    """The instance is in first run, and its `AdGuardSetup` has not configured it in this run."""


def gone() -> dynamic.ReadResult:
    """What `read` returns for a resource the instance does not hold.

    Dropping the identifier is how the engine learns the resource is gone. The
    outputs are an empty bag rather than `None`, and a fresh one each time,
    because the dynamic-provider host writes its own bookkeeping key into
    whatever bag it is handed.
    """
    return dynamic.ReadResult(id_=None, outs={})


def unknown_anywhere(value: Any) -> bool:
    """Whether `value` holds a preview placeholder at any depth."""
    if is_unknown(value):
        return True
    if isinstance(value, Mapping):
        return any(unknown_anywhere(item) for item in value.values())  # pyright: ignore[reportUnknownVariableType]
    if isinstance(value, Sequence) and not isinstance(value, str):
        return any(unknown_anywhere(item) for item in value)  # pyright: ignore[reportUnknownVariableType]
    return False


def missing_or_extra(
    prop: str, value: Any, keys: frozenset[str], failures: list[dynamic.CheckFailure], *, label: str | None = None
) -> bool:
    """Record a failure where `value` is not an object holding exactly `keys`; return whether it is one.

    The instance reads a key a request leaves out as its zero value, so a key
    missing from a declared object would be written as `false`, `0` or `""`
    with nothing said; a key it does not have is dropped as silently. `label`
    names the object inside `prop` where it is not `prop` itself.
    """
    named = label or prop
    if not isinstance(value, Mapping):
        failures.append(dynamic.CheckFailure(prop, f'{named} is not an object'))
        return False
    present = {str(key) for key in cast('Mapping[str, Any]', value)}
    if missing := sorted(keys - present):
        failures.append(dynamic.CheckFailure(prop, f'{named} lacks {", ".join(missing)}'))
    if extra := sorted(present - keys):
        failures.append(dynamic.CheckFailure(prop, f'{named} has no key {", ".join(extra)}'))
    return not missing and not extra


class InstanceProvider(ConfiguredProvider):
    """What every kind shares: one instance, one login, and an offline `diff`."""

    #: The kind, as the resource id spells it after the instance.
    kind: ClassVar[str]

    #: The sections the kind declares, each one top-level property.
    sections: ClassVar[tuple[str, ...]]

    username: str
    password: str

    def _read_credential(self, config: dynamic.Config) -> None:
        self.username = str(config.require(USERNAME_CONFIG))
        self.password = str(config.require(PASSWORD_CONFIG))

    def _credential(self) -> str:
        """Both halves: moving to another admin account is as much a rotation as a new password."""
        return f'{self.username}:{self.password}'

    def _endpoint(self, props: Mapping[str, Any]) -> str:
        return str(props[ENDPOINT]).rstrip('/')

    def _version(self) -> str:
        return VERSION

    @abc.abstractmethod
    def _comparable(self, section: str, value: Any) -> object:
        """`value`, a section as declared or as read, in the form two equal ones share.

        It keeps only the keys the kind declares, so a field the instance
        reports and the declaration does not name is never compared.
        """

    def _refusals(self, _news: Mapping[str, Any], _failures: list[dynamic.CheckFailure]) -> None:
        """Record what the instance would refuse and can be told offline. Every section is known here."""
        return

    def check(self, _olds: dict[str, Any], news: dict[str, Any]) -> dynamic.CheckResult:
        failures: list[dynamic.CheckFailure] = []
        if not any(unknown_anywhere(news.get(section)) for section in self.sections):
            self._refusals(news, failures)
        return self._stamp(news, failures)

    def diff(self, _id: str, olds: dict[str, Any], news: dict[str, Any]) -> dynamic.DiffResult:
        replaces = [INSTANCE] if not is_unknown(news.get(INSTANCE)) and olds.get(INSTANCE) != news.get(INSTANCE) else []
        if unknown_anywhere(news):
            return dynamic.DiffResult(changes=None, replaces=replaces, delete_before_replace=False)
        changed = bool(self._changed(olds, news)) or any(olds.get(key) != news.get(key) for key in RECORDED)
        return dynamic.DiffResult(changes=changed, replaces=replaces, delete_before_replace=False)

    def delete(self, _id: str, _props: dict[str, Any]) -> None:
        return

    def _changed(self, olds: Mapping[str, Any] | None, news: Mapping[str, Any]) -> list[str]:
        """The sections of `news` that differ from `olds`: every one where there are no `olds`."""
        if olds is None:
            return list(self.sections)
        return [
            section
            for section in self.sections
            if self._comparable(section, olds.get(section)) != self._comparable(section, news.get(section))
        ]

    def _verdict(self, props: Mapping[str, Any]) -> Verdict:
        """The instance a property bag names, classified for the configured login; raises where it is unusable."""
        return classify(self._endpoint(props), str(props[SETUP_ENDPOINT]), self.username, self.password)

    def _api(self, props: Mapping[str, Any]) -> Api:
        """The instance a property bag names, opened with the configured login."""
        return Api(self._endpoint(props), self.username, self.password)


class AdGuardProvider(InstanceProvider):
    """One kind's operations against the configuration of one running instance."""

    @abc.abstractmethod
    def _read(self, api: Api) -> dict[str, Any]:
        """Every section, as the instance holds it, in the declared shape."""

    @abc.abstractmethod
    def _write(self, api: Api, news: Mapping[str, Any], olds: Mapping[str, Any] | None) -> None:
        """Bring the instance to `news`.

        `olds` is the stored output bag on an update and `None` on a create.
        """

    def create(self, props: dict[str, Any]) -> dynamic.CreateResult:
        self._write(self._configured(props), props, None)
        # The checked inputs go back out as the outputs, stamps included, so
        # the stored bag records the login that wrote the instance.
        return dynamic.CreateResult(id_=f'{props[INSTANCE]}|{self.kind}', outs=props)

    def read(self, id_: str, props: dict[str, Any]) -> dynamic.ReadResult:
        if self._verdict(props) is Verdict.FIRST_RUN:
            return gone()
        live = self._read(self._api(props))
        carried = {key: props[key] for key in RECORDED if key in props}
        # Two bags, because the provider host writes its own key into each.
        return dynamic.ReadResult(id_=id_, outs={**carried, **live}, inputs={**carried, **live})

    def update(self, _id: str, olds: dict[str, Any], news: dict[str, Any]) -> dynamic.UpdateResult:
        if self._changed(olds, news):
            self._write(self._configured(news), news, olds)
        # The outs replace the stored output bag (framework/pulumi.md §5.3 E9),
        # so what state says about the door the instance was written through
        # stays true.
        return dynamic.UpdateResult(outs=news)

    def _configured(self, props: Mapping[str, Any]) -> Api:
        """The instance opened for a write, which only a configured one takes."""
        if self._verdict(props) is Verdict.FIRST_RUN:
            raise SetupNotRun(
                f'{props[INSTANCE]} is in first run: it holds no configuration and no account, and its '
                'AdGuardSetup has not configured it in this run. Run again with --refresh, which drops its '
                "resources and re-creates them after the instance's setup."
            )
        return self._api(props)
