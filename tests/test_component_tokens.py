"""Every component class and every dynamic resource class states its own type token, in the form this installation gives one.

A type token is part of the URN of a component and of every resource beneath
it, so it is chosen rather than computed (style/pulumi.md, a type token is
chosen). `putils.Component` refuses to construct a class that states none; the
census here holds every component class the installation defines to having
stated one, and to its form, before anything is constructed, so a class no
other suite builds is held too. A dynamic resource's token is the SDK's
`pulumi-python:dynamic` followed by the `module` and `name` its class statement
gives, and the SDK fills in `Resource` for a class that gives none, so the
census is the only thing that refuses one.
"""

import importlib
import inspect
import pkgutil
import re
from collections import Counter
from pathlib import Path

import pytest
from pulumi import dynamic

import kluster
from kluster.components.dns.zone import ManagedZone
from kluster.components.forge import ManagedRepository
from kluster.components.gateway import Gateway
from kluster.components.talos.image import TalosArtifact, TalosImage
from kluster.providers.adguard import AdGuardUserRules
from kluster.providers.adguard_rewrites import AdGuardRewrite
from kluster.providers.device_files.provider import DeviceFile
from kluster.providers.talos_factory import FactoryImage
from putils import Component, UnstatedTypeError

#: The form style/pulumi.md gives a token: `kluster`, the area of the design
#: the kind belongs to, spelled as its package is, underscores and all, and
#: the kind's own name.
FORM = re.compile(r'kluster:[a-z][a-z_]*:[A-Z][A-Za-z0-9]*')

#: The form style/pulumi.md gives a dynamic resource's token: the package and
#: module the SDK fixes, the `kluster.providers` package the kind belongs to,
#: and the kind's own name.
DYNAMIC_FORM = re.compile(r'pulumi-python:dynamic/[a-z][a-z_]*:[A-Z][A-Za-z0-9]*')

#: A dynamic resource's token as a document spells one of a class, anywhere in
#: its text: one with a module, which the SDK's own default has not.
DYNAMIC = re.compile(r'pulumi-python:dynamic/[\w/]+:\w+')

ROOT = Path(__file__).parent.parent

#: A URN as the documents spell one, up to the first type of its chain: stack,
#: project, then the type of the resource or of its outermost component.
URN = re.compile(r'urn:pulumi:[^:\s]+::[^:\s]+::([^$\s\']+?)(?:\$|::)')


def _below[T](cls: type[T]) -> list[type[T]]:
    return [each for sub in cls.__subclasses__() for each in (sub, *_below(sub))]


def _defined[T](base: type[T]) -> list[type[T]]:
    """Every subclass of `base` the installation defines, found by importing every module under `kluster`."""
    for module in pkgutil.walk_packages(kluster.__path__, f'{kluster.__name__}.'):
        if module.name.rpartition('.')[2] != '__main__':
            importlib.import_module(module.name)
    found = dict.fromkeys(cls for cls in _below(base) if cls.__module__.startswith(f'{kluster.__name__}.'))
    return list(found)


def components() -> list[type[Component]]:
    """Every component class the installation defines."""
    return _defined(Component)


def dynamic_resources() -> list[type[dynamic.Resource]]:
    """Every dynamic resource class the installation defines."""
    return _defined(dynamic.Resource)


def stated(cls: type[Component]) -> str | None:
    """The token `cls` states in its own class statement, never one a base states."""
    token = vars(cls).get('__pulumi_type__')
    return token if isinstance(token, str) else None


def dynamic_stated(cls: type[dynamic.Resource]) -> str:
    """The token `cls` registers under: `pulumi-python:`, then what the SDK made of its class statement."""
    # The SDK's `__init_subclass__` sets this on every subclass, to
    # `dynamic:Resource` for one whose statement gives neither `module` nor
    # `name`; read from the class itself, it is never a base's.
    return f'pulumi-python:{vars(cls)["_resource_type_name"]}'


def test_the_census_reaches_components_of_every_kind() -> None:
    # The positive control: a walk that imported nothing would find no class,
    # and every case over its result would pass vacuously. One component with
    # state behind it, one declared by a stack that has none, a subclass of an
    # abstract base, and the abstract base itself.
    found = components()

    assert {ManagedRepository, ManagedZone, Gateway, TalosImage, TalosArtifact} <= set(found)


def test_every_component_class_states_its_own_token_in_the_installations_form() -> None:
    unstated = [
        f'{cls.__module__}.{cls.__qualname__}: {stated(cls)!r}'
        for cls in components()
        if not inspect.isabstract(cls) and not FORM.fullmatch(stated(cls) or '')
    ]

    assert unstated == [], unstated


@pytest.mark.parametrize(
    'token',
    ['kluster:state-backend:StateBackend', 'kluster:StateBackend:StateBackend', 'kluster:_state:StateBackend'],
    ids=['dashed', 'uppercase', 'leading-underscore'],
)
def test_an_area_no_package_can_be_named_is_off_the_form(token: str) -> None:
    assert not FORM.fullmatch(token)


def test_an_area_named_with_an_underscore_is_on_the_form() -> None:
    assert FORM.fullmatch('kluster:state_backend:StateBackend')


def test_no_two_component_classes_share_a_token() -> None:
    # Two kinds under one token are one type to the engine: the URN of each
    # is then the other's for any shared name, and neither can be told apart
    # in state.
    tokens = Counter(token for cls in components() if (token := stated(cls)) is not None)

    assert [token for token, count in tokens.items() if count > 1] == []


def test_an_abstract_base_states_no_token() -> None:
    # It is never registered, so a token on it names nothing; and a token it
    # stated would be the one a subclass that forgot its own reads by lookup.
    assert stated(TalosArtifact) is None


def test_a_class_that_states_no_token_is_refused_when_it_is_constructed() -> None:
    class Unstated(Component):
        pass

    with pytest.raises(UnstatedTypeError, match=r'Unstated states no type token of its own'):
        Unstated('unstated')


def test_a_subclass_does_not_take_its_base_token() -> None:
    class Stated(Component, pulumi_type='test:Stated'):
        pass

    class Inherits(Stated):
        pass

    with pytest.raises(UnstatedTypeError, match=r'Inherits states no type token of its own'):
        Inherits('inherits')


def test_every_urn_a_document_spells_names_a_type_a_component_states() -> None:
    # A document that targets or imports by URN spells a component's type,
    # and one naming a type no class states selects nothing without erroring.
    # The documents are the side of this seam the program does not write.
    tokens = {token for cls in components() if (token := stated(cls)) is not None}
    spelled = {
        (str(path.relative_to(ROOT)), outermost)
        for path in (ROOT / 'docs').rglob('*.md')
        for outermost in URN.findall(path.read_text())
        if outermost.startswith('kluster:')
    }

    assert spelled, 'no document spells a component URN; the pattern is what broke'
    assert sorted((path, outermost) for path, outermost in spelled if outermost not in tokens) == []


def test_the_census_reaches_a_dynamic_resource_of_every_provider() -> None:
    # The positive control for the dynamic half: one class from each provider
    # package that defines one.
    found = dynamic_resources()

    assert {DeviceFile, FactoryImage, AdGuardRewrite, AdGuardUserRules} <= set(found)


def test_every_dynamic_resource_class_states_its_token_in_the_installations_form() -> None:
    off = [
        f'{cls.__module__}.{cls.__qualname__}: {dynamic_stated(cls)!r}'
        for cls in dynamic_resources()
        if not DYNAMIC_FORM.fullmatch(dynamic_stated(cls))
    ]

    assert off == [], off


def test_a_dynamic_resource_that_states_no_token_is_off_the_form() -> None:
    # The SDK registers such a class under `dynamic:Resource`, which every
    # other unstated class shares; the form is what tells it apart.
    class Unstated(dynamic.Resource):
        pass

    assert dynamic_stated(Unstated) == 'pulumi-python:dynamic:Resource'
    assert not DYNAMIC_FORM.fullmatch(dynamic_stated(Unstated))


def test_no_two_dynamic_resource_classes_share_a_token() -> None:
    tokens = Counter(dynamic_stated(cls) for cls in dynamic_resources())

    assert [token for token, count in tokens.items() if count > 1] == []


def test_every_dynamic_type_a_document_spells_is_one_a_class_states() -> None:
    # The RFCs are accepted records and keep the tokens they were written
    # against; every other document describes the tree as it is.
    tokens = {dynamic_stated(cls) for cls in dynamic_resources()}
    spelled = {
        (str(path.relative_to(ROOT)), token)
        for path in (ROOT / 'docs').rglob('*.md')
        if path.parent.name != 'rfc'
        for token in DYNAMIC.findall(path.read_text())
    }

    assert spelled, 'no document spells a dynamic resource type; the pattern is what broke'
    assert sorted((path, token) for path, token in spelled if token not in tokens) == []
