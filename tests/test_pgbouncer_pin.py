"""The appliance's PgBouncer image is pinned the way its Postgres image is.

The pin -- the repository, the release's tag and the digest the box runs --
is read by one renovate manager as the settings hold it, a move of its digest
replaces the box and is taken in the image bases' monthly window, in the
appliance's group, and `state-backend pins` asks the registry the image lives
in, GitHub's, whether it serves the pinned digest.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import cast
from urllib import request as urllib_request

import pytest
from renovate_text import as_python_spells_it, group, listed, package_rules

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
    """A digest move replaces the box, so it is taken in the image bases' monthly window, with the appliance's pins."""
    config = (ROOT / 'renovate.json5').read_text()
    (found,) = as_python_spells_it(IMAGE_MATCH_STRING).finditer((ROOT / APPLIANCE).read_text())
    (rule,) = [
        rule
        for rule in package_rules(config)
        if listed(rule, 'matchDepNames') == [found['depName']] and listed(rule, 'matchUpdateTypes') == ['digest']
    ]
    (bases,) = [
        rule
        for rule in package_rules(config)
        if listed(rule, 'matchFileNames') == ['docker/**'] and listed(rule, 'matchUpdateTypes') == ['digest']
    ]

    assert listed(rule, 'schedule') == listed(bases, 'schedule') != []
    # Grouped by the rule for the file the manager reads it from, which the
    # narrower rule leaves alone.
    assert group(rule) == []
    assert group(_appliance_rule(config)) != []


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
