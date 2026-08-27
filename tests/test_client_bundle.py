"""What a client bundle has to be for `pulumi login` to work at all.

The bundle is three files and a connection string, and the string is the part
that used to be wrong: it named `$KLUSTER_PG_CA` and friends, which nothing on
the path expands -- not libpq, not the driver Pulumi's Postgres backend uses.
The placeholder reached `open()` verbatim and login failed on a missing file,
so logging in to the backend was not possible from the bundle at all.
"""

from __future__ import annotations

from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from kluster.scripts.credentials import pki
from kluster.scripts.state_backend import config


@pytest.fixture
def authority() -> pki.Authority:
    return pki.Authority.from_pem(pki.generate_ca_key())


def _query(url: str) -> dict[str, str]:
    return {key: value[0] for key, value in parse_qs(urlsplit(url).query).items()}


def test_the_url_names_files_that_exist(authority: pki.Authority, tmp_path: Path) -> None:
    bundle = config.client_bundle(authority, name='operator', address='192.0.2.10')
    config.write_client_bundle(bundle, tmp_path)

    url = (tmp_path / config.URL_FILE).read_text().strip()
    assert '$' not in url
    query = _query(url)
    for parameter in ('sslrootcert', 'sslcert', 'sslkey'):
        assert Path(query[parameter]).is_file(), parameter


def test_the_url_pins_the_server_by_address(authority: pki.Authority, tmp_path: Path) -> None:
    # verify-full against a literal IP: the state backend's hot path must not
    # depend on DNS, which is itself something this backend deploys.
    config.write_client_bundle(config.client_bundle(authority, name='ci', address='192.0.2.10'), tmp_path)

    url = (tmp_path / config.URL_FILE).read_text().strip()
    assert url.startswith('postgres://ci@192.0.2.10:5432/')
    assert _query(url)['sslmode'] == 'verify-full'


def test_the_private_key_is_not_world_readable(authority: pki.Authority, tmp_path: Path) -> None:
    # libpq refuses a key with group or world permissions.
    config.write_client_bundle(config.client_bundle(authority, name='operator', address='192.0.2.10'), tmp_path)

    assert (tmp_path / config.KEY_FILE).stat().st_mode & 0o077 == 0


def test_the_directory_it_lands_in_is_the_operators_alone(authority: pki.Authority, tmp_path: Path) -> None:
    # The bundle is a workstation slot now (credentials.md §1 rule 6), so it
    # is created like one: a 0755 directory over a 0600 key still tells
    # anybody with a shell that the key is there and what it is called.
    directory = tmp_path / 'made' / 'here'

    config.write_client_bundle(config.client_bundle(authority, name='operator', address='192.0.2.10'), directory)

    assert directory.stat().st_mode & 0o777 == 0o700
    assert directory.parent.stat().st_mode & 0o777 == 0o700
