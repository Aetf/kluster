"""The rewrite provider's CRUD, against a stand-in for one AdGuard instance."""

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
        self.payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> object:
        return self.payload


class FakeSession:
    """One instance's rewrite list, plus a log of what was asked of it."""

    entries: list[dict[str, str]] = []
    posts: list[tuple[str, dict[str, str]]] = []

    def __init__(self) -> None:
        self.auth: tuple[str, str] | None = None

    def get(self, url: str, timeout: int = 0) -> FakeResponse:
        assert url.endswith('/control/rewrite/list')
        return FakeResponse(list(FakeSession.entries))

    def post(self, url: str, json: dict[str, str], timeout: int = 0) -> FakeResponse:
        FakeSession.posts.append((url.rsplit('/', 1)[-1], json))
        if url.endswith('/add'):
            FakeSession.entries.append(json)
        else:
            FakeSession.entries.remove(json)
        return FakeResponse({})


@pytest.fixture(autouse=True)
def session(monkeypatch: pytest.MonkeyPatch) -> None:
    FakeSession.entries = []
    FakeSession.posts = []
    monkeypatch.setattr(requests, 'Session', FakeSession)


def _provider() -> adguard_rewrites.AdGuardRewriteProvider:
    return adguard_rewrites.AdGuardRewriteProvider()


def test_create_adds_the_pair_and_ids_it_by_instance() -> None:
    result = _provider().create(dict(PROPS))

    assert FakeSession.entries == [{'domain': 'photos.ucw.phd', 'answer': '192.168.71.1'}]
    # The id names the instance: the same rewrite on alice and on bob are two
    # resources, because they are two writes.
    assert result.id == f'{ENDPOINT}|photos.ucw.phd|192.168.71.1'


def test_create_adopts_an_identical_entry_rather_than_duplicating_it() -> None:
    """AdGuard stores duplicates, and duplicates cannot be deleted apart.

    Which is what a retried `up` after a partial failure would produce.
    """
    FakeSession.entries = [{'domain': 'photos.ucw.phd', 'answer': '192.168.71.1'}]

    _ = _provider().create(dict(PROPS))

    assert FakeSession.posts == []


def test_read_reports_a_hand_removed_rewrite_as_gone() -> None:
    # Which is how a rewrite deleted in the UI is restored by the next up
    # instead of drifting unnoticed.
    result = _provider().read('any', dict(PROPS))

    assert result.id is None
    # The provider host writes its own key into the outs and mutates the
    # dict, so gone must come back as a fresh empty dict, never None.
    assert result.outs == {}


def test_read_keeps_a_rewrite_that_is_still_there() -> None:
    FakeSession.entries = [{'domain': 'photos.ucw.phd', 'answer': '192.168.71.1'}]

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


def test_delete_removes_exactly_the_declared_pair() -> None:
    FakeSession.entries = [
        {'domain': 'photos.ucw.phd', 'answer': '192.168.71.1'},
        {'domain': 'tube.ucw.phd', 'answer': '192.168.71.1'},
    ]

    _provider().delete('an-id', dict(PROPS))

    assert FakeSession.entries == [{'domain': 'tube.ucw.phd', 'answer': '192.168.71.1'}]
    assert FakeSession.posts[0][0] == 'delete'


def test_the_instance_label_is_a_resource_name_fragment() -> None:
    assert adguard.instance_label('http://alice.lan:3000') == 'alice-lan'
