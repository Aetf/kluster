"""The rewrite provider's CRUD, against a stand-in for one AdGuard instance.

**A provider under test is configured first**, because a provider in production
is: the plugin deserializes it out of a resource's `__provider` property and
calls `configure` before handing it any operation (rfc-002 §7.5 E2). `provider`
below does that with a `ConfigureRequest` built the way the plugin builds one --
the same class, the same project namespace -- so what the tests exercise is the
real ordering rather than attributes set by hand.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from typing import Any

import pulumi.dynamic as dynamic
import pytest
import requests

# The engine's own provider serialization, which is what a `__provider` property
# holds. It lives beside the base class rather than in the package's exports.
from pulumi.dynamic.dynamic import serialize_provider  # pyright: ignore[reportUnknownVariableType]
from pulumi.runtime import rpc

from kluster.components.dns import adguard
from kluster.providers import adguard_rewrites, configured

ENDPOINT = 'http://alice.lan:3000'
USERNAME = 'admin'
PASSWORD = 'a-typed-secret'

#: The project the configuration keys below are namespaced by. An unqualified
#: key is resolved against the running project, which is how the plugin finds
#: it (rfc-002 §7.5 E2).
PROJECT = 'kluster'

PROPS: dict[str, Any] = {
    'endpoint': ENDPOINT,
    'domain': 'photos.ucw.phd',
    'answer': '192.168.71.1',
}


class FakeResponse:
    def __init__(self, payload: object) -> None:
        self.payload: object = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self.payload


@dataclass
class Instance:
    """One AdGuard instance: its rewrite list, and a log of what was asked of it."""

    #: The rewrites the instance holds, in the order it holds them.
    entries: list[dict[str, str]] = field(default_factory=list[dict[str, str]])
    #: Every write, as `(endpoint name, body)`.
    posts: list[tuple[str, dict[str, str]]] = field(default_factory=list[tuple[str, dict[str, str]]])
    #: Every session opened onto it, so a test can ask what it authenticated as.
    opened: list[FakeSession] = field(default_factory=list['FakeSession'])

    def session(self) -> FakeSession:
        """A `requests.Session` onto this instance, as the provider builds one."""
        served = FakeSession(self)
        self.opened.append(served)
        return served


class FakeSession:
    def __init__(self, instance: Instance) -> None:
        self.instance: Instance = instance
        self.auth: tuple[str, str] | None = None

    def get(self, url: str, timeout: int = 0) -> FakeResponse:
        assert url.endswith('/control/rewrite/list')
        return FakeResponse(list(self.instance.entries))

    def post(self, url: str, json: dict[str, str], timeout: int = 0) -> FakeResponse:
        self.instance.posts.append((url.rsplit('/', 1)[-1], json))
        if url.endswith('/add'):
            self.instance.entries.append(json)
        else:
            self.instance.entries.remove(json)
        return FakeResponse({})


@pytest.fixture(autouse=True)
def instance(monkeypatch: pytest.MonkeyPatch) -> Instance:
    """The instance the provider reaches, empty unless a case fills it."""
    served = Instance()
    monkeypatch.setattr(requests, 'Session', served.session)
    return served


def provider(password: str = PASSWORD) -> adguard_rewrites.AdGuardRewriteProvider:
    """A provider as an operation receives one: revived, then handed the config.

    The login arrives already decrypted, which is what the plugin does with a
    secret configuration value before calling `configure`.
    """
    instance = adguard_rewrites.AdGuardRewriteProvider()
    instance.configure(
        dynamic.ConfigureRequest(
            config=dynamic.Config(
                {
                    f'{PROJECT}:{adguard_rewrites.USERNAME_CONFIG}': USERNAME,
                    f'{PROJECT}:{adguard_rewrites.PASSWORD_CONFIG}': password,
                },
                PROJECT,
            )
        )
    )
    return instance


def checked(props: dict[str, Any], password: str = PASSWORD) -> dict[str, Any]:
    """The inputs as the engine stores and compares them: what `check` returned."""
    return provider(password).check({}, props).inputs


def test_create_adds_the_pair_and_ids_it_by_instance(instance: Instance) -> None:
    result = provider().create(dict(PROPS))

    assert instance.entries == [{'domain': 'photos.ucw.phd', 'answer': '192.168.71.1'}]
    # The id names the instance: the same rewrite on alice and on bob are two
    # resources, because they are two writes.
    assert result.id == f'{ENDPOINT}|photos.ucw.phd|192.168.71.1'


def test_create_adopts_an_identical_entry_rather_than_duplicating_it(instance: Instance) -> None:
    """AdGuard stores duplicates, and duplicates cannot be deleted apart.

    Which is what a retried `up` after a partial failure would produce.
    """
    instance.entries = [{'domain': 'photos.ucw.phd', 'answer': '192.168.71.1'}]

    _ = provider().create(dict(PROPS))

    assert instance.posts == []


def test_a_write_authenticates_as_the_configured_login(instance: Instance) -> None:
    """The instance is declared; the login that answers it is not.

    It comes from `configure`, so no property bag carries it and no caller
    could have passed a different one.
    """
    _ = provider().create(dict(PROPS))

    assert [opened.auth for opened in instance.opened] == [(USERNAME, PASSWORD)]


def test_read_reports_a_hand_removed_rewrite_as_gone() -> None:
    # Which is how a rewrite deleted in the UI is restored by the next up
    # instead of drifting unnoticed.
    result = provider().read('any', dict(PROPS))

    assert result.id is None
    # The provider host writes its own key into the outs and mutates the
    # dict, so gone must come back as a fresh empty dict, never None.
    assert result.outs == {}


def test_read_keeps_a_rewrite_that_is_still_there(instance: Instance) -> None:
    instance.entries = [{'domain': 'photos.ucw.phd', 'answer': '192.168.71.1'}]

    assert provider().read('an-id', dict(PROPS)).id == 'an-id'


def test_a_changed_answer_replaces_without_a_gap() -> None:
    """There is no update endpoint, and deleting first is a LAN outage.

    Two rewrites for one name coexist harmlessly for the instant between the
    create and the delete; no answer at all does not.
    """
    changed = checked(dict(PROPS) | {'answer': '192.168.71.2'})

    result = provider().diff('an-id', checked(dict(PROPS)), changed)

    assert result.changes is True
    assert result.replaces == ['answer']
    assert result.delete_before_replace is False


def test_an_input_that_is_still_unknown_is_an_unknown_diff_and_plans_no_replacement(instance: Instance) -> None:
    """Every declared property is a replacement, so an unknown one must not be read.

    During a preview an answer may be another resource's unresolved output. A
    placeholder compared as a value differs from whatever is stored, which here
    would plan a delete and a create of a row about to be identical.
    """
    news = checked(dict(PROPS) | {'answer': rpc.UNKNOWN})

    result = provider().diff('an-id', checked(dict(PROPS)), news)

    assert result.changes is None
    assert not result.replaces
    assert instance.opened == []


def test_a_rotated_login_is_a_change_nobody_declared() -> None:
    """The point of the session stamp: a rotation is a diff with no program in it.

    No caller mentions the login, so the only thing that can carry a rotation
    into a preview is a property the provider adds to the checked inputs
    itself. It is the same row on the same instance, so it is not a replace.
    """
    olds = checked(dict(PROPS))
    news = checked(dict(PROPS), password='rotated')

    result = provider().diff('an-id', olds, news)

    assert result.changes is True
    assert result.replaces == []


def test_a_re_stamp_records_the_new_login_and_calls_the_instance_not_at_all(instance: Instance) -> None:
    """A rotation must not rewrite every row on both instances.

    The row the instance holds is the row the resource declares, so there is
    nothing to write; what the update does is record which login the resource
    is now written through.
    """
    olds = checked(dict(PROPS))
    news = checked(dict(PROPS), password='rotated')

    result = provider().update('an-id', olds, news)

    assert result.outs == news
    assert instance.opened == []


def test_the_session_stamp_names_the_instance_and_fingerprints_the_login() -> None:
    """`http://alice.lan:3000#<12 hex>` — the door, and which login opens it.

    The digest is what a preview shows on a rotation, so it is stored in the
    clear: a truncated digest of a login is not the login, and a redacted one
    would say only that something opaque changed.
    """
    session = checked(dict(PROPS))[configured.SESSION]
    endpoint, _, fingerprint = session.partition('#')

    assert endpoint == ENDPOINT
    assert fingerprint == hashlib.sha256(f'{USERNAME}:{PASSWORD}'.encode()).hexdigest()[: configured.FINGERPRINT_LENGTH]
    assert PASSWORD not in session


def test_a_change_to_this_module_is_a_change_a_reader_can_see(monkeypatch: pytest.MonkeyPatch) -> None:
    """A provider is pickled by reference, so editing an operation moves nothing.

    The version constant is what makes such an edit an update instead of a
    silent no-op that leaves every resource's outputs as the old code left them.
    """
    shipped = adguard_rewrites.VERSION
    olds = checked(dict(PROPS))
    monkeypatch.setattr(adguard_rewrites, 'VERSION', f'{shipped}-next')
    news = checked(dict(PROPS))

    assert olds[configured.PROVIDER_VERSION] == shipped
    assert news[configured.PROVIDER_VERSION] == f'{shipped}-next'
    assert provider().diff('an-id', olds, news).changes is True


def test_what_lands_in_state_is_a_provider_with_nothing_in_it() -> None:
    """Every resource stores a pickle of its provider; this one is a name.

    Serialized through the engine's own function, so what is asserted is what a
    `__provider` property would actually hold. A class imported from a module is
    pickled by reference and `__getstate__` returns an empty bag, so state
    carries something inert: identical for every rewrite, identical across a
    rotation, and holding nothing that a rotation would have to reach into.
    """
    one = serialize_provider(provider())
    rotated = serialize_provider(provider('rotated'))

    assert provider().__getstate__() == {}
    assert one == rotated
    assert PASSWORD not in one
    assert len(one) < 256


def test_a_provider_that_was_never_configured_has_no_login_to_dial_with() -> None:
    """The attributes exist only after `configure`, and that is the design.

    A default would not make an unconfigured provider safe; it would make one
    that dials with the wrong login. The plugin configures before the first
    operation, so nothing in production sees this state.
    """
    with pytest.raises(AttributeError):
        _ = adguard_rewrites.AdGuardRewriteProvider().password


def test_a_missing_half_of_the_login_refuses_by_name() -> None:
    """A half-filled configuration stops the run rather than the session."""
    half = dynamic.Config({f'{PROJECT}:{adguard_rewrites.USERNAME_CONFIG}': USERNAME}, PROJECT)

    with pytest.raises(ValueError, match=adguard_rewrites.PASSWORD_CONFIG):
        adguard_rewrites.AdGuardRewriteProvider().configure(dynamic.ConfigureRequest(config=half))


def test_delete_removes_exactly_the_declared_pair(instance: Instance) -> None:
    instance.entries = [
        {'domain': 'photos.ucw.phd', 'answer': '192.168.71.1'},
        {'domain': 'tube.ucw.phd', 'answer': '192.168.71.1'},
    ]

    provider().delete('an-id', dict(PROPS))

    assert instance.entries == [{'domain': 'tube.ucw.phd', 'answer': '192.168.71.1'}]
    assert instance.posts[0][0] == 'delete'


def test_the_instance_label_is_a_resource_name_fragment() -> None:
    assert adguard.instance_label('http://alice.lan:3000') == 'alice-lan'
