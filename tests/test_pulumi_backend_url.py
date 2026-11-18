"""The forms of a bundle's one connection string, and which reader is given which.

A client bundle holds one connection string, and `kluster.lib.bundle` reads
it in the form its reader names (`bundle.Purpose`). `pulumi` is given it with
`binary_parameters=yes` added to its query (`bundle.pulumi_url`), a parameter
of the `lib/pq` driver behind Pulumi's Postgres backend; `psql` is given it
as written, because libpq refuses that parameter; `pg_dump` and `pg_restore`
are given it naming the pooler's session entry (`bundle.dump_url`). Held here
for the Python side: every path that starts `pulumi` from a slot, and every
tool `state` runs.
`mise.toml`'s spelling of the same rule is `tests/test_mise_env.py`'s, and a
real `pulumi` against the pinned Postgres through the URL it is given is
`tests/test_state_roles.py`'s, whose `credentials` run against `github`
creates a stack and reads it back through `BackendEnvironment.variables`.
"""

from __future__ import annotations

import shutil
import subprocess as sp
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import pytest
from root_credentials import fake

from kluster.conventions import identity
from kluster.lib import bundle, pulumi_cli, stack_environment
from kluster.lib.state_backend import settings, state, table
from kluster.scripts.credentials import pulumi_config

#: The connection string `state-backend bundle` writes.
URL = 'postgres://operator@192.0.2.10:5432/pulumi_state?sslmode=verify-full'

#: The shapes a bundle's connection string comes in: as written, with no
#: query, and naming its certificate files in the query, as an older bundle
#: does.
SHAPES = {
    'as written': URL,
    'no query': 'postgres://operator@192.0.2.10:5432/pulumi_state',
    'file parameters': f'{URL}&sslrootcert=/b/ca.crt&sslcert=/b/client.crt&sslkey=/b/client.key',
}

#: A stack CI deploys, whose backend a `credentials` run is pointed at by the
#: URL it read from the slot.
CI_STACK = identity.STACK_NAMES.dns


def _carries_the_parameter(url: str) -> bool:
    return ('binary_parameters', 'yes') in parse_qsl(urlsplit(url).query)


@pytest.mark.parametrize('written', list(SHAPES.values()), ids=list(SHAPES))
def test_the_pulumi_url_is_the_bundles_with_binary_parameters_added(written: str) -> None:
    given = urlsplit(bundle.pulumi_url(written))
    original = urlsplit(written)

    # The backend, the role and the database are the bundle's, and every
    # parameter it wrote is still there, in its order, before the one added.
    assert given._replace(query='') == original._replace(query='')
    assert parse_qsl(given.query) == [*parse_qsl(original.query), ('binary_parameters', 'yes')]


@pytest.mark.parametrize('url', ['', 'file:///scratch/state?metadata=skip'], ids=['empty', 'file'])
@pytest.mark.parametrize('purpose', list(bundle.Purpose), ids=[purpose.value for purpose in bundle.Purpose])
def test_a_url_that_is_no_postgres_backend_is_left_as_it_is(url: str, purpose: bundle.Purpose) -> None:
    # The parameter and the entry are the Postgres backend's: an empty URL
    # names no backend, and a `file://` backend refuses a query parameter it
    # does not know.
    assert bundle.form(url, purpose) == url


@pytest.mark.parametrize('written', list(SHAPES.values()), ids=list(SHAPES))
def test_the_dump_url_is_the_bundles_naming_the_session_entry(written: str) -> None:
    given, original = urlsplit(bundle.dump_url(written)), urlsplit(written)

    assert given.path == f'/{settings.SESSION_DATABASE}'
    assert given._replace(path='') == original._replace(path='')


@pytest.mark.parametrize('written', list(SHAPES.values()), ids=list(SHAPES))
def test_a_url_in_the_pulumi_form_is_in_it_once(written: str) -> None:
    # A caller handed the form already -- a run whose URL a reader formed --
    # passes it on unchanged.
    once = bundle.pulumi_url(written)

    assert bundle.pulumi_url(once) == once


@pytest.mark.parametrize('written', list(SHAPES.values()), ids=list(SHAPES))
def test_the_reader_reads_the_one_stored_string_in_each_purposes_form(tmp_path: Path, written: str) -> None:
    _ = (tmp_path / bundle.URL_FILE).write_text(written + '\n')

    assert {purpose: bundle.read(tmp_path, purpose) for purpose in bundle.Purpose} == {
        bundle.Purpose.PULUMI: bundle.pulumi_url(written),
        bundle.Purpose.QUERY: written,
        bundle.Purpose.DUMP: bundle.dump_url(written),
    }


def test_the_reader_tells_no_bundle_from_an_empty_one(tmp_path: Path) -> None:
    assert bundle.read(tmp_path, bundle.Purpose.PULUMI) is None
    _ = (tmp_path / bundle.URL_FILE).write_text('\n')
    assert bundle.read(tmp_path, bundle.Purpose.PULUMI) == ''


@pytest.mark.parametrize('stack', [CI_STACK, identity.STACK_NAMES.github])
def test_a_credentials_run_against_a_stack_in_the_backend_is_given_the_pulumi_url(tmp_path: Path, stack: str) -> None:
    # The URL a `credentials` command read from the slot, as written, goes in;
    # what `pulumi` is started with carries the parameter, for a stack CI
    # deploys and for the operator stack whose state is in the backend alike.
    assert stack_environment.home(stack) in (None, identity.StateHome.BACKEND)
    environment = pulumi_config.BackendEnvironment(
        passphrase=lambda: fake(pulumi_config.PASSPHRASE_ENV),
        operator=lambda: fake(stack_environment.OPERATOR_PASSPHRASE_ENV),
        url=URL,
        bundle_dir=tmp_path,
    )

    given = environment.variables(stack)[pulumi_config.BACKEND_URL_ENV]

    assert given == bundle.pulumi_url(URL)
    assert _carries_the_parameter(given)


def test_a_committed_stack_is_given_its_checkpoints_untouched(tmp_path: Path) -> None:
    # The parameter is the Postgres driver's; a `file://` backend is not
    # formed from a slot and gets none.
    stack = identity.STACK_NAMES.state_backend
    assert stack_environment.home(stack) is identity.StateHome.COMMITTED

    given = stack_environment.backend_variables(stack, checkout=tmp_path, estate_url=URL)

    assert given[stack_environment.BACKEND_URL_ENV] == stack_environment.committed_url(tmp_path)


class Tools:
    """`subprocess.run` as `state` calls it, recording each libpq tool's command line.

    `pg_dump` leaves an archive where it was told to, `psql` prints an empty
    census, and `pg_restore` prints nothing: what each is asked to connect to
    is the subject, not what it does there.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, argv: Sequence[str], **_options: Any) -> sp.CompletedProcess[str]:
        self.calls.append(list(argv))
        stdout = ''
        if Path(argv[0]).name == Path(state.PG_DUMP).name:
            destination = next(value.removeprefix('--file=') for value in argv if value.startswith('--file='))
            _ = Path(destination).write_bytes(state.ARCHIVE_MAGIC)
        elif Path(argv[0]).name == Path(table.PSQL).name:
            stdout = '[]'
        return sp.CompletedProcess(args=list(argv), returncode=0, stdout=stdout, stderr='')

    def dbnames(self) -> Mapping[str, str]:
        """The connection string each tool was handed, by tool."""
        return {
            Path(argv[0]).name: value.removeprefix('--dbname=')
            for argv in self.calls
            for value in argv
            if value.startswith('--dbname=')
        }


@pytest.mark.parametrize('written', list(SHAPES.values()), ids=list(SHAPES))
def test_each_libpq_tool_is_handed_the_form_its_purpose_reads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, written: str
) -> None:
    # The slot a `pulumi` run's URL is formed from is the one a dump, a
    # restore and the census read; the census is handed the URL as the slot
    # holds it, and the dump and the restore the form naming the session
    # entry. libpq accepts both, and neither carries the driver's parameter.
    slot = tmp_path / 'state-backend'
    slot.mkdir()
    _ = (slot / bundle.URL_FILE).write_text(written + '\n')
    tools = Tools()
    monkeypatch.setattr(table.sp, 'run', tools)
    target = state.connection(slot)

    state.pg_dump(target, tmp_path / 'taken.dump')
    state.pg_restore(target, tmp_path / 'taken.dump')
    assert state.census(target) == []

    assert tools.dbnames() == {
        Path(state.PG_DUMP).name: bundle.dump_url(written),
        Path(state.PG_RESTORE).name: bundle.dump_url(written),
        Path(table.PSQL).name: written,
    }
    assert not any(_carries_the_parameter(url) for url in tools.dbnames().values())


@pytest.mark.skipif(shutil.which(table.PSQL) is None, reason='psql is not on PATH (mise x -- ...)')
def test_the_pinned_libpq_refuses_the_url_pulumi_is_given_and_takes_the_bundles() -> None:
    # Why the two are kept apart, held to the client `mise.toml` pins: libpq
    # reads a connection string before it opens anything, so the URL `pulumi`
    # is given is refused by name, while the bundle's goes on to connect --
    # to an address where nothing listens, so that is where it stops.
    plain = 'postgres://nobody@127.0.0.1:1/nothing?sslmode=disable'

    with pytest.raises(state.StateError, match='invalid URI query parameter') as refused:
        _ = state.census(state.Connection(url=bundle.pulumi_url(plain), env={}))
    assert 'binary_parameters' in str(refused.value)
    with pytest.raises(state.StateError) as unreached:
        _ = state.census(state.Connection(url=plain, env={}))
    assert 'invalid URI query parameter' not in str(unreached.value)
    assert '127.0.0.1' in str(unreached.value)


def test_the_stacks_question_is_asked_through_the_pulumi_form(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # `state.stacks` runs `pulumi stack ls`, so it is a `pulumi` session like
    # any other, and is handed the URL `pulumi` is.
    slot = tmp_path / 'state-backend'
    slot.mkdir()
    _ = (slot / bundle.URL_FILE).write_text(URL + '\n')
    given: list[str | None] = []

    def run_pulumi(_args: Sequence[str], *, env: Mapping[str, str], **_options: Any) -> str:
        given.append(env.get(stack_environment.BACKEND_URL_ENV))
        return '[]'

    monkeypatch.setattr(pulumi_cli, 'run_pulumi', run_pulumi)

    assert state.stacks(state.connection(slot)) == []
    assert given == [bundle.pulumi_url(URL)]
