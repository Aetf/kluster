"""The appliance's PgBouncer image is pinned the way its Postgres image is.

The pin -- the repository, the release's tag and the digest the box runs --
is read by one renovate manager as the settings hold it, a move of its digest
replaces the box, so the rule for those moves gives them a window of one day
a month, beside the Postgres image's and apart from the appliance's releases,
and `state-backend pins` asks the registry the image lives in, GitHub's,
whether it serves the pinned digest.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast
from urllib import request as urllib_request

import pytest
from renovate_text import (
    as_python_spells_it,
    effective,
    enabled,
    listed,
    reaches,
    scalar,
)

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
    """The rule grouping the appliance's releases: the one a minor release of the pooler takes its branch from."""
    release = {
        'matchDepNames': (settings.PGBOUNCER_REPOSITORY,),
        'matchPackageNames': (settings.PGBOUNCER_REPOSITORY,),
        'matchManagers': ('custom.regex', 'regex'),
        'matchFileNames': (APPLIANCE,),
        'matchDatasources': ('docker',),
        'matchUpdateTypes': ('minor',),
    }
    rule = effective(config, release, 'groupSlug')
    assert rule is not None, 'no rule names the branch a release of the appliance travels on'
    return rule


def test_renovate_reads_the_image_the_box_runs() -> None:
    """The image manager captures the repository, tag and digest the pooler's reference is made of."""
    config = (ROOT / 'renovate.json5').read_text()
    assert config.count('"' + IMAGE_MATCH_STRING.replace('\\', '\\\\') + '"') == 1

    (found,) = as_python_spells_it(IMAGE_MATCH_STRING).finditer((ROOT / APPLIANCE).read_text())

    assert f'{found["depName"]}:{found["currentValue"]}@{found["currentDigest"]}' == settings.PGBOUNCER_IMAGE


def test_the_images_digest_moves_monthly_apart_from_the_appliances_releases() -> None:
    """A digest move replaces the box, so renovate offers one on one day of every month, apart from the releases.

    The release's tag is rebuilt on each new base, and each move of the pin
    is a dump, a new box and a restore, as a move of the Postgres image's is
    (`tests/test_postgres_pin.py`). A window on more days than one a month
    puts that replacement up for merging after each rebuild; one that skips a
    month leaves a base's security fix off the box for longer than a month.
    In the releases' group the window would not hold: that group's branch
    takes the window of the update it sorts first, and a release takes none
    (`tests/test_postgres_pin.py`, every update on an appliance branch
    carrying that branch's window).

    What renovate gives the update is the last matching rule that sets each
    key, so a later, broader rule setting `schedule` or a group overrides the
    narrow one; the update is resolved against every rule, the way renovate
    merges them. Held as text, because there is no JSON5 parser here
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

    grouping = effective(config, digest, 'groupSlug')
    assert grouping is not None, "no rule names the digest's branch"
    assert scalar(grouping, 'groupSlug') != scalar(_appliance_rule(config), 'groupSlug'), (
        "the digest moves on the releases' branch, on whichever update's window sorts first"
    )


def test_a_patch_release_of_the_pooler_is_offered() -> None:
    """Renovate proposes a PgBouncer patch release, which is where PgBouncer ships its security fixes.

    The top-level `patch` block disables every patch update, and a matching
    rule setting `enabled` overrides it (`renovate_text.enabled`), so a
    patch of the pooler is offered only where such a rule turns it back on.
    The pooler is the box's one listener on the network, and a patch left
    for the next minor leaves a fix of its pre-authentication code off the
    box. The rule is the box's: a pin of the same image in another file is
    left to the global block. Held as text, because there is no JSON5 parser
    here (`renovate_text`).
    """
    config = (ROOT / 'renovate.json5').read_text()
    (found,) = as_python_spells_it(IMAGE_MATCH_STRING).finditer((ROOT / APPLIANCE).read_text())
    patch = {
        'matchDepNames': (found['depName'],),
        'matchPackageNames': (found['depName'],),
        'matchManagers': ('custom.regex', 'regex'),
        'matchFileNames': (APPLIANCE,),
        'matchDatasources': ('docker',),
        'matchUpdateTypes': ('patch',),
    }

    assert enabled(config, patch), 'the global patch block drops every PgBouncer patch release, and no rule offers one'
    enabling = effective(config, patch, 'enabled')
    elsewhere = {key: value for key, value in patch.items() if key != 'matchManagers'}
    elsewhere['matchFileNames'] = ('src/kluster/lib/state_backend/images.py',)
    assert enabling is None or not reaches(enabling, elsewhere), (
        "the rule offering the box's PgBouncer patches offers them to a pin of the image in any file"
    )


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
