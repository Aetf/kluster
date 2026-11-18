"""Renovate reads the local Postgres client's pin, and moves its major with the appliance's.

`mise.toml`'s `postgres` and `settings.POSTGRES_IMAGE` are held to one major
(`tests/test_postgres_client.py`), so a major release has to move both in one
pull request: apart, each half fails that test on its own. The image is in
the appliance's group by its file; `renovate.json5` puts the client's major
there too, and leaves its minor releases in the toolchain group, since the
image's tag names no minor.

The image's own pin -- the repository, the major line's tag and the digest
the box runs -- is read by one manager as the settings hold it, and a move of
its digest replaces the box, so the rule for those moves gives them a window
of one day a month, in a group of the appliance's image refreshes apart from
its releases. Renovate opens a group's branch on the window of the update it
sorts first, so every update on either branch carries that branch's one
window.

The client is read at all only through a rule of its own. The mise manager
resolves the pin to the conda datasource under the bare package name, which
api.anaconda.org does not answer, so the rule names the package as
`<channel>/<name>`, with the channel `mise.toml` writes.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import cast

from renovate_text import (
    as_python_spells_it,
    effective,
    group,
    listed,
    matchers,
    matches,
    package_rule,
    package_rules,
    reaches,
    scalar,
    string_value,
)

from kluster.lib.state_backend import settings

ROOT = Path(__file__).parent.parent

#: The key `mise.toml` pins the client under, which is the name the mise
#: manager gives the dependency.
PIN = 'postgres'

#: The conda package mise's registry resolves `postgres` to (`mise registry
#: postgres` answers `conda:postgresql`).
PACKAGE = 'postgresql'

#: The file every pin of the appliance lives in, which renovate groups by.
APPLIANCE = 'src/kluster/lib/state_backend/settings.py'

#: The image manager's pattern, as Python writes it out: the repository, the
#: tag and the digest on three consecutive lines, captured together.
IMAGE_MATCH_STRING = (
    r"POSTGRES_REPOSITORY = '(?<depName>[\w./-]+)'\nPOSTGRES_TAG = '(?<currentValue>[\w.-]+)'"
    r"\nPOSTGRES_DIGEST = '(?<currentDigest>sha256:[0-9a-f]{64})'"
)


def _for_the_client(rule: str) -> bool:
    return listed(rule, 'matchDatasources') == ['conda'] and listed(rule, 'matchDepNames') == [PIN]


def _client_rules(config: str) -> list[str]:
    return [rule for rule in package_rules(config) if _for_the_client(rule)]


def _appliance_rule(config: str) -> str:
    """The rule grouping the appliance's releases: the one a release of the Postgres image takes its branch from."""
    rule = effective(config, APPLIANCE_UPDATES['Postgres image major'], 'groupSlug')
    assert rule is not None, 'no rule names the branch a release of the appliance travels on'
    return rule


def test_renovate_reads_the_client_from_the_channel_mise_installs_it_from() -> None:
    """Held as text, because there is no JSON5 parser here (`renovate_text`)."""
    config = (ROOT / 'renovate.json5').read_text()
    pin = cast('dict[str, str]', tomllib.loads((ROOT / 'mise.toml').read_text())['tools'][PIN])

    named = [rule for rule in _client_rules(config) if scalar(rule, 'overridePackageName') is not None]
    assert len(named) == 1, 'no rule names the conda package, so renovate looks up a name nothing answers'
    (rule,) = named
    assert matchers(rule) == ['matchDatasources', 'matchDepNames']
    assert scalar(rule, 'overridePackageName') == f'{pin["channel"]}/{PACKAGE}'


def test_renovate_moves_the_clients_major_with_the_appliance() -> None:
    """Held as text, because there is no JSON5 parser here (`renovate_text`)."""
    config = (ROOT / 'renovate.json5').read_text()
    appliance_rule = _appliance_rule(config)

    grouping = [
        (start, rule)
        for start, rule in {package_rule(config, hit.start()) for hit in re.finditer("'conda'", config)}
        if _for_the_client(rule) and group(rule)
    ]
    assert len(grouping) == 1, "no rule puts the client's major in the appliance's group"
    ((at, rule),) = grouping
    assert matchers(rule) == ['matchDatasources', 'matchDepNames', 'matchUpdateTypes']
    assert listed(rule, 'matchUpdateTypes') == ['major']
    assert len(group(appliance_rule)) == 2
    assert group(rule) == group(appliance_rule)

    # The last match wins a field, so the rule sits after the one grouping the
    # whole mise manager as the toolchain.
    (toolchain_at,) = [
        start
        for start, found in (package_rule(config, hit.start()) for hit in re.finditer("'mise'", config))
        if not listed(found, 'matchDepNames')
    ]
    assert toolchain_at < at


def test_renovate_reads_the_image_the_box_runs() -> None:
    """The image manager captures the repository, tag and digest the box's reference is made of."""
    config = (ROOT / 'renovate.json5').read_text()
    assert config.count('"' + IMAGE_MATCH_STRING.replace('\\', '\\\\') + '"') == 1

    (found,) = as_python_spells_it(IMAGE_MATCH_STRING).finditer((ROOT / APPLIANCE).read_text())

    assert f'{found["depName"]}:{found["currentValue"]}@{found["currentDigest"]}' == settings.POSTGRES_IMAGE


def test_the_images_digest_moves_monthly_apart_from_the_appliances_releases() -> None:
    """A digest move replaces the box, so renovate offers one on one day of every month, apart from the releases.

    Docker Hub rebuilds the major line's tag for every minor release and every
    new Debian base, and each move of the pin is a dump, a new box and a
    restore. A window on more days than one a month puts that replacement up
    for merging after each rebuild; one that skips a month leaves a Postgres
    security release off the box for longer than a month. In the releases'
    group the window would not hold: that group's branch takes the window of
    the update it sorts first, and a release takes none
    (`test_every_update_on_an_appliance_branch_carries_that_branchs_window`).

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


#: The updates renovate can make of the appliance's pins, each as it presents
#: itself to a rule's matchers: each pin the custom managers read off the
#: appliance's files (`test_every_pin_of_the_appliance_is_an_update_listed`),
#: and the two `mise.toml` pins rules take into the appliance's group. An
#: update has one type, so each type a pin can move by is an update of its
#: own; a type a rule disables is listed all the same, since an update listed
#: in excess only adds to what is checked. A value an update would present
#: that is not settled here is left out, and a matcher with nothing presented
#: to it is taken as matching (`renovate_text.reaches`).
_RELEASES = ('major', 'minor', 'patch')
_IN_SETTINGS = {'matchManagers': ('custom.regex', 'regex'), 'matchFileNames': (APPLIANCE,)}
_IN_MISE = {'matchManagers': ('mise',), 'matchFileNames': ('mise.toml',)}
APPLIANCE_UPDATES: dict[str, dict[str, tuple[str, ...]]] = {
    **{
        f'{name} {kind}': {
            'matchDepNames': (repository,),
            'matchPackageNames': (repository,),
            'matchDatasources': ('docker',),
            'matchUpdateTypes': (kind,),
            **_IN_SETTINGS,
        }
        for name, repository in (
            ('Postgres image', settings.POSTGRES_REPOSITORY),
            ('PgBouncer image', settings.PGBOUNCER_REPOSITORY),
        )
        for kind in ('digest', *_RELEASES)
    },
    **{
        f'Fedora CoreOS {kind}': {
            'matchDepNames': ('fedora-coreos',),
            'matchPackageNames': ('fedora-coreos',),
            'matchDatasources': ('custom.fedora-coreos-stable',),
            'matchUpdateTypes': (kind,),
            **_IN_SETTINGS,
        }
        for kind in ('digest', *_RELEASES)
    },
    **{
        f'settings age {kind}': {
            'matchDepNames': ('FiloSottile/age',),
            'matchPackageNames': ('FiloSottile/age',),
            'matchDatasources': ('github-releases',),
            'matchUpdateTypes': (kind,),
            **_IN_SETTINGS,
        }
        for kind in _RELEASES
    },
    **{
        f'mise age {kind}': {
            'matchDepNames': ('age',),
            # The aqua backend's data source, and the one the age rule overrides it with.
            'matchDatasources': ('github-tags', 'github-releases'),
            'matchUpdateTypes': (kind,),
            **_IN_MISE,
        }
        for kind in _RELEASES
    },
    'client major': {
        'matchDepNames': (PIN,),
        'matchDatasources': ('conda',),
        'matchUpdateTypes': ('major',),
        **_IN_MISE,
    },
}


def _branch(config: str, update: dict[str, tuple[str, ...]]) -> str | None:
    """The group slug that names the update's branch: the last matching rule's that sets one."""
    rule = effective(config, update, 'groupSlug')
    return None if rule is None else scalar(rule, 'groupSlug')


def _window(config: str, update: dict[str, tuple[str, ...]]) -> tuple[str, ...]:
    """The cron entries the update carries, from the last matching rule that sets them; none for no window."""
    rule = effective(config, update, 'schedule')
    return () if rule is None else tuple(listed(rule, 'schedule'))


def test_every_update_on_an_appliance_branch_carries_that_branchs_window() -> None:
    """Every update on either of the appliance's branches carries one window, and the releases' branch none.

    Renovate opens a group's branch, or updates it, on the schedule of the
    update it sorts first in the group (`generate.ts`, the branch config
    taken from `upgrades[0]`), so a window held by some of a branch's updates
    and not by others holds on the days the sort favors it: a rebuild of an
    image would travel on a release's day, and a release would wait for the
    rebuilds' window. One window per branch is what makes the sort
    immaterial. The releases' branch takes none, so a release is offered the
    day renovate finds it. Every update listed travels on one of the two,
    since a pin of the appliance moving on a branch of its own is a
    replacement of the box that the appliance's pull requests do not show.

    The two branches are the appliance rule's and the one its rebuilds take,
    and each rule that names either is reached by some update listed here, so
    a rule bringing a dependency onto either branch names one this check
    does not hold until it is listed; a pin the appliance rule brings in by
    its file is held to the list by the census below, and a pin of the same
    dependencies in any other file is held off both branches by the case
    after this one. Held as text, because there is no JSON5 parser here
    (`renovate_text`).
    """
    config = (ROOT / 'renovate.json5').read_text()
    releases = scalar(_appliance_rule(config), 'groupSlug')
    rebuilds = _branch(config, APPLIANCE_UPDATES['Postgres image digest'])
    assert releases is not None
    assert rebuilds is not None

    windows: dict[str, set[tuple[str, ...]]] = {releases: set(), rebuilds: set()}
    for name, update in APPLIANCE_UPDATES.items():
        branch = _branch(config, update)
        assert branch in windows, f'the {name} travels on {branch}, apart from the appliance'
        windows[branch].add(_window(config, update))
    for branch, carried in windows.items():
        assert len(carried) == 1, f'{branch} opens on the window of whichever update sorts first: {carried}'
    assert windows[releases] == {()}, f'a release of the appliance is held to a window: {windows[releases]}'

    naming = [rule for rule in package_rules(config) if scalar(rule, 'groupSlug') in windows]
    for rule in naming:
        assert any(effective(config, update, 'groupSlug') == rule for update in APPLIANCE_UPDATES.values()), (
            f'a rule brings onto an appliance branch an update this check does not hold:\n{rule}'
        )


#: Files the appliance rule does not match that a pin of one of its
#: dependencies could sit in: a module beside the settings, in their package,
#: and a settings module of another package.
ELSEWHERE = ('src/kluster/lib/state_backend/images.py', 'src/kluster/lib/settings.py')


def test_a_pin_of_the_appliances_dependencies_in_another_file_joins_neither_branch() -> None:
    """The census reads only the appliance's files, which covers every update on its branches because no other can join.

    A second pin of the Postgres image in another file, with a rule giving it
    a window of its own, would otherwise join the refreshes' branch by name,
    and the branch would open on whichever window sorts first, unseen by the
    census, which reads the appliance's files, and by the check above, which
    holds the updates listed. So no rule naming either branch can reach an
    update of a pin the custom managers read off the settings, presented
    from a file the appliance rule does not match and by any manager. That
    is a miss `renovate_text.reaches` proves, whichever rule renovate would
    take the update's group from. Held as text, because there is no JSON5
    parser here (`renovate_text`).
    """
    config = (ROOT / 'renovate.json5').read_text()
    patterns = listed(_appliance_rule(config), 'matchFileNames')
    branches = {
        scalar(_appliance_rule(config), 'groupSlug'),
        _branch(config, APPLIANCE_UPDATES['Postgres image digest']),
    }
    assert None not in branches
    naming = [rule for rule in package_rules(config) if scalar(rule, 'groupSlug') in branches]

    for path in ELSEWHERE:
        assert not any(matches(pattern, path) for pattern in patterns), f'the appliance rule matches {path}'
        for name, update in APPLIANCE_UPDATES.items():
            if update['matchFileNames'] != (APPLIANCE,):
                continue
            stray = {key: value for key, value in update.items() if key != 'matchManagers'}
            stray['matchFileNames'] = (path,)
            joining = [rule for rule in naming if reaches(rule, stray)]
            assert not joining, f'a {name} pinned in {path} can join an appliance branch:\n' + '\n'.join(joining)


def _appliance_files(config: str) -> list[str]:
    """Every file under the appliance's package that the appliance rule's file patterns match."""
    patterns = listed(_appliance_rule(config), 'matchFileNames')
    package = ROOT / 'src/kluster/lib/state_backend'
    paths = [path.relative_to(ROOT).as_posix() for path in sorted(package.rglob('*')) if path.is_file()]
    return [path for path in paths if any(matches(pattern, path) for pattern in patterns)]


def _custom_managers(config: str) -> list[str]:
    """Each `customManagers` entry of `renovate.json5`, as text.

    An entry is cut at the next `customType:`, since a match string may carry
    the braces a brace scan would cut at.
    """
    block = config[config.index('customManagers: [') : config.index('customDatasources: {')]
    return block.split('customType:')[1:]


def test_every_pin_of_the_appliance_is_an_update_listed() -> None:
    """Each dependency a custom manager reads off the appliance's files is one `APPLIANCE_UPDATES` presents.

    The appliance rule groups by file, so a pin added to the settings with a
    manager of its own joins the releases' branch with no rule naming it, and
    the check above would not see it. A manager's dependency is the name its
    template gives, or the `depName` its match strings capture in the files it
    reads; a manager whose dependency this census cannot read fails it.
    """
    config = (ROOT / 'renovate.json5').read_text()
    files = _appliance_files(config)
    assert APPLIANCE in files
    listed_names = {name for update in APPLIANCE_UPDATES.values() for name in update['matchDepNames']}

    for entry in _custom_managers(config):
        read = [
            path for path in files if any(matches(pattern, path) for pattern in listed(entry, 'managerFilePatterns'))
        ]
        if not read:
            continue
        template = scalar(entry, 'depNameTemplate')
        if template is not None:
            names = {string_value(template)}
        else:
            names = {
                found['depName']
                for written in listed(entry, 'matchStrings')
                for path in read
                for found in as_python_spells_it(string_value(written)).finditer((ROOT / path).read_text())
            }
        assert names, f'a manager on the appliance reads a dependency this census cannot name:\n{entry}'
        assert names <= listed_names, f'{sorted(names - listed_names)} move on an appliance branch unlisted'
