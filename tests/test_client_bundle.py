"""What a client bundle has to be for `pulumi login` to work at all.

The bundle is three files and a connection string, and the split between them
is the property these tests hold. The string says only what is true of the
backend everywhere -- who connects, to which address, under which TLS mode --
because the copy recorded beside the bundle outlives the directory it was
written in: a checkout that is moved, or a bundle that is copied to a second
workstation, must not be carrying a path that no longer exists. Where the
three files are is the environment's job (`PGSSL*`), and nothing expands a
placeholder written into the string itself -- not libpq, not the driver
Pulumi's Postgres backend uses.
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


def test_the_url_carries_nothing_about_the_machine_it_was_written_on(authority: pki.Authority, tmp_path: Path) -> None:
    bundle = config.client_bundle(authority, name='operator', address='192.0.2.10')
    config.write_client_bundle(bundle, tmp_path)

    url = (tmp_path / config.URL_FILE).read_text().strip()
    assert '$' not in url
    assert str(tmp_path) not in url
    assert set(_query(url)) == {'sslmode'}


def test_the_environment_names_the_three_files_and_they_are_there(authority: pki.Authority, tmp_path: Path) -> None:
    # The other half of the connection: the paths libpq reads from the
    # environment, which is the channel that survives a placeholder being
    # expanded by nobody.
    config.write_client_bundle(config.client_bundle(authority, name='operator', address='192.0.2.10'), tmp_path)

    variables = config.ssl_env(tmp_path)

    assert set(variables) == {'PGSSLROOTCERT', 'PGSSLCERT', 'PGSSLKEY'}
    for name, value in variables.items():
        assert Path(value).is_absolute(), name
        assert Path(value).is_file(), name


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
