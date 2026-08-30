"""The rewrite provider's CRUD, against a stand-in for one AdGuard instance."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import pytest
import requests

from kluster.components.dns import adguard
from kluster.providers import adguard_rewrites

ENDPOINT = 'http://alice.lan:3000'
PROPS: dict[str, Any] = {
    'endpoint': ENDPOINT,
    'username': 'admin',
    'password': 'secret',
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

    def session(self) -> FakeSession:
        """A `requests.Session` onto this instance, as the provider builds one."""
        return FakeSession(self)


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


def _provider() -> adguard_rewrites.AdGuardRewriteProvider:
    return adguard_rewrites.AdGuardRewriteProvider()


def test_create_adds_the_pair_and_ids_it_by_instance(instance: Instance) -> None:
    result = _provider().create(dict(PROPS))

    assert instance.entries == [{'domain': 'photos.ucw.phd', 'answer': '192.168.71.1'}]
    # The id names the instance: the same rewrite on alice and on bob are two
    # resources, because they are two writes.
    assert result.id == f'{ENDPOINT}|photos.ucw.phd|192.168.71.1'


def test_create_adopts_an_identical_entry_rather_than_duplicating_it(instance: Instance) -> None:
    """AdGuard stores duplicates, and duplicates cannot be deleted apart.

    Which is what a retried `up` after a partial failure would produce.
    """
    instance.entries = [{'domain': 'photos.ucw.phd', 'answer': '192.168.71.1'}]

    _ = _provider().create(dict(PROPS))

    assert instance.posts == []


def test_read_reports_a_hand_removed_rewrite_as_gone() -> None:
    # Which is how a rewrite deleted in the UI is restored by the next up
    # instead of drifting unnoticed.
    result = _provider().read('any', dict(PROPS))

    assert result.id is None
    # The provider host writes its own key into the outs and mutates the
    # dict, so gone must come back as a fresh empty dict, never None.
    assert result.outs == {}


def test_read_keeps_a_rewrite_that_is_still_there(instance: Instance) -> None:
    instance.entries = [{'domain': 'photos.ucw.phd', 'answer': '192.168.71.1'}]

    assert _provider().read('an-id', dict(PROPS)).id == 'an-id'


def test_a_changed_answer_replaces_without_a_gap() -> None:
    """There is no update endpoint, and deleting first is a LAN outage.

    Two rewrites for one name coexist harmlessly for the instant between the
    create and the delete; no answer at all does not.
    """
    changed = dict(PROPS) | {'answer': '192.168.71.2'}

    result = _provider().diff('an-id', dict(PROPS), changed)

    assert result.changes is True
    assert result.replaces == ['answer']
    assert result.delete_before_replace is False


def test_a_rotated_credential_is_a_change_but_not_a_replace() -> None:
    # The rewrite is the same row; only the way it is written changed.
    result = _provider().diff('an-id', dict(PROPS), dict(PROPS) | {'password': 'rotated'})

    assert result.changes is True
    assert result.replaces == []


def test_delete_removes_exactly_the_declared_pair(instance: Instance) -> None:
    instance.entries = [
        {'domain': 'photos.ucw.phd', 'answer': '192.168.71.1'},
        {'domain': 'tube.ucw.phd', 'answer': '192.168.71.1'},
    ]

    _provider().delete('an-id', dict(PROPS))

    assert instance.entries == [{'domain': 'tube.ucw.phd', 'answer': '192.168.71.1'}]
    assert instance.posts[0][0] == 'delete'


def test_the_instance_label_is_a_resource_name_fragment() -> None:
    assert adguard.instance_label('http://alice.lan:3000') == 'alice-lan'
