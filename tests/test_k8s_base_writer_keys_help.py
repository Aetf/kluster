"""The help names every writer key `k8s-base` declares wherever it says how one is sealed early.

The sitting that seals the cluster's own values seals every value `k8s-base`
reads, each of its writer keys by a `b2-writer seal --only` run of its own.
Two help texts tell the operator so: the bring-up order in `credentials
--help`, which is written by hand, and the help of `b2-writer seal --only`.
Each case here is a writer key taken from the census (`conventions.sealed`),
so a key that comes to be declared in `k8s-base` is a case without anyone
editing this file, and a help text that leaves it out fails that case: an
operator who follows it would finish the sitting with that key unsealed, and,
once the bring-up's tolerance is gone, `k8s-base` stopped at the read.
"""

from __future__ import annotations

import re

import pytest
from credentials_command_tree import tree

from kluster import conventions
from kluster.scripts.credentials import cli

#: Wide enough that argparse wraps no option's help, so a name that holds a
#: hyphen is never split across two lines of the rendering.
WIDTH = '1000'


def k8s_base_writer_keys() -> list[str]:
    """Every writer key whose readers `k8s-base` declares, by name, read off the census."""
    return [
        value.name
        for value in conventions.sealed.WRITER_KEYS_BY_SCOPE.values()
        if value.stack == conventions.STACK_NAMES.k8s_base
    ]


def test_the_census_walk_finds_the_keys() -> None:
    # A walk that found nothing would leave the cases below with no
    # parameters, which pytest reports as skipped rather than failed.
    assert conventions.sealed.ETCD_WRITER_KEY.name in k8s_base_writer_keys()


@pytest.mark.parametrize('name', k8s_base_writer_keys())
def test_the_bring_up_order_seals_each_key_on_its_own(name: str) -> None:
    order = cli.build_parser().epilog or ''

    sealed = re.findall(r'credentials derived b2-writer seal --only (\S+)', order)

    assert name in sealed, (
        f'`k8s-base` declares what reads `{name}`, but the order in `credentials --help` never seals it on its '
        "own: an operator who follows that order would leave the sitting that seals the cluster's own values "
        'without it.'
    )


@pytest.mark.parametrize('name', k8s_base_writer_keys())
def test_the_only_option_names_each_key(name: str, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv('COLUMNS', WIDTH)
    parsers = dict(tree(cli.build_parser()))

    rendered = parsers['derived', 'b2-writer', 'seal'].format_help()

    named = re.search(rf'(?<![\w-]){re.escape(name)}(?![\w-])', rendered)

    assert named, f'`credentials derived b2-writer seal --help` does not name `{name}`.'
