"""The appliance's PgBouncer image is pinned the way its Postgres image is.

The pin -- the repository, the release's tag and the digest the box runs --
is read by one renovate manager as the settings hold it, a move of its digest
replaces the box, so the rule for those moves gives them a window of one day
a month, in the appliance's group, and `state-backend pins` asks the registry
the image lives in, GitHub's, whether it serves the pinned digest.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast
from urllib import request as urllib_request

import pytest
from renovate_text import as_python_spells_it, effective, group, listed, package_rules

from kluster.lib.state_backend import settings
from kluster.scripts.state_backend import provision

ROOT = Path(__file__).parent.parent

#: The file every pin of the appliance lives in, which renovate groups by.
APPLIANCE = 'src/kluster/lib/state_backend/settings.py'

#: The image manager's pattern, as Python writes it out: the repository, the
#: tag and the digest on three consecutive lines, captured together.
IMAGE_MATCH_STRING = (
    r"PGBOUNCER_REPOSITORY = '(?<depName>[\w./-]+)'\nPGBOUNCER_TAG = '(?<currentValue>[\w.-]+)'"
    r"\nPGBOUNCER_DIGEST = '(?<currentDigest>sha256:[0-9a-f]{64})'"
)


def _appliance_rule(config: str) -> str:
    """The rule grouping every pin of the appliance's settings: the one matching that file and setting a group."""
    (rule,) = [rule for rule in package_rules(config) if APPLIANCE in listed(rule, 'matchFileNames') and group(rule)]
    return rule


def test_renovate_reads_the_image_the_box_runs() -> None:
    """The image manager captures the repository, tag and digest the pooler's reference is made of."""
    config = (ROOT / 'renovate.json5').read_text()
    assert config.count('"' + IMAGE_MATCH_STRING.replace('\\', '\\\\') + '"') == 1

    (found,) = as_python_spells_it(IMAGE_MATCH_STRING).finditer((ROOT / APPLIANCE).read_text())

    assert f'{found["depName"]}:{found["currentValue"]}@{found["currentDigest"]}' == settings.PGBOUNCER_IMAGE


def test_the_images_digest_moves_monthly_in_the_appliances_group() -> None:
    """A digest move replaces the box, so renovate offers one on one day of every month, in the appliance's group.

    The release's tag is rebuilt on each new base, and each move of the pin
    is a dump, a new box and a restore, as a move of the Postgres image's is
    (`tests/test_postgres_pin.py`). A window on more days than one a month
    puts that replacement up for merging after each rebuild; one that skips a
    month leaves a base's security fix off the box for longer than a month.
    Grouped elsewhere, the replacement travels in a pull request not read as
    one.

    What renovate gives the update is the last matching rule that sets each
    key, so a later, broader rule setting `schedule` or a group overrides the
    narrow one; the update is resolved against every rule, the way renovate
    merges them. The window held is the update's own: renovate opens a
    group's branch on the schedule of the update it sorts first, so beside
    another of the appliance's updates a digest move can travel on that one's
    schedule. Held as text, because there is no JSON5 parser here
    (`renovate_text`).
    """
    config = (ROOT / 'renovate.json5').read_text()
    (found,) = as_python_spells_it(IMAGE_MATCH_STRING).finditer((ROOT / APPLIANCE).read_text())
    # The custom manager reads the image from the appliance's settings, off Docker's data source.
    digest = {
        'matchDepNames': (found['depName'],),
        'matchPackageNames': (found['depName'],),
        'matchManagers': ('custom.regex', 'regex'),
        'matchFileNames': (APPLIANCE,),
        'matchDatasources': ('docker',),
        'matchUpdateTypes': ('digest',),
    }

    scheduling = effective(config, digest, 'schedule')
    assert scheduling is not None, 'no rule gives the digest a window, so each rebuild opens a pull request'
    windows = [entry.split() for entry in listed(scheduling, 'schedule')]
    assert windows, f'the window renovate takes is not a list of cron entries:\n{scheduling}'
    for window in windows:
        # Minute, hour, day of the month, month, day of the week: any hours of
        # one day that every month has, in every month, whatever its weekday.
        assert len(window) == 5, f'not a cron entry: {window}'
        minute, _hours, day, month, weekday = window
        assert (minute, month, weekday) == ('*', '*', '*'), f'not every month, or not every weekday: {window}'
        assert day.isdigit(), f'more than one day a month: {window}'
        assert 1 <= int(day) <= 28, f'a day some month does not have: {window}'
    assert len({day for _minute, _hours, day, _month, _weekday in windows}) == 1, (
        f'more than one day a month: {windows}'
    )

    assert effective(config, digest, 'groupName') == _appliance_rule(config)
    assert effective(config, digest, 'groupSlug') == _appliance_rule(config)


class _Answer:
    """One `urlopen` answer: a JSON body, or headers and no body."""

    def __init__(self, *, body: object = None, headers: dict[str, str] | None = None) -> None:
        self.headers: dict[str, str] = headers or {}
        self._body: bytes = json.dumps(body).encode() if body is not None else b''

    def read(self) -> bytes:
        return self._body

    def __enter__(self) -> _Answer:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


def test_the_pins_check_asks_the_registry_the_image_lives_in(monkeypatch: pytest.MonkeyPatch) -> None:
    """The token and the manifest are asked of GitHub's registry, for the image's name there."""
    requested: list[str] = []
    answers = [
        _Answer(body={'token': 'a-pull-token'}),
        _Answer(headers={provision.DIGEST_HEADER: settings.PGBOUNCER_DIGEST}),
    ]

    def urlopen(request: object, *_args: object, **_kwargs: object) -> _Answer:
        requested.append(request if isinstance(request, str) else cast('urllib_request.Request', request).full_url)
        return answers.pop(0)

    monkeypatch.setattr(provision.urllib_request, 'urlopen', urlopen)
    resolved = provision._image_digest(settings.PGBOUNCER_REPOSITORY, settings.PGBOUNCER_DIGEST)  # pyright: ignore[reportPrivateUsage]

    name = settings.PGBOUNCER_REPOSITORY.removeprefix('ghcr.io/')
    assert resolved == settings.PGBOUNCER_DIGEST
    assert requested == [
        f'https://ghcr.io/token?scope=repository:{name}:pull',
        f'https://ghcr.io/v2/{name}/manifests/{settings.PGBOUNCER_DIGEST}',
    ]


@pytest.mark.parametrize(
    ('served', 'passes'),
    [(settings.PGBOUNCER_DIGEST, True), ('sha256:' + 'f' * 64, False)],
    ids=['the-pin', 'another-digest'],
)
def test_the_pins_check_holds_the_pooler_to_the_digest_the_registry_serves(
    monkeypatch: pytest.MonkeyPatch, served: str, passes: bool
) -> None:
    """`pins` asks for the pooler's own pin, its digest and its tag, and fails on a digest the registry does not serve."""
    asked: list[tuple[str, str]] = []

    def image_digest(repository: str, reference: str) -> str:
        asked.append((repository, reference))
        return served

    monkeypatch.setattr(provision, '_image_digest', image_digest)

    assert provision._pgbouncer_pin_ok() is passes  # pyright: ignore[reportPrivateUsage]
    assert set(asked) == {
        (settings.PGBOUNCER_REPOSITORY, settings.PGBOUNCER_DIGEST),
        (settings.PGBOUNCER_REPOSITORY, settings.PGBOUNCER_TAG),
    }
