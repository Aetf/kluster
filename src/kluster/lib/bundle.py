"""A state-backend client bundle's layout in its slot.

A client bundle is a directory: the CA certificate, a client certificate and
its key, and the connection string for the backend they authenticate against.
The files' names, and the environment variables that name them to a client,
are read by every party that touches a bundle -- the `state-backend` command
that writes one, the `credentials` commands that open a `pulumi` run with one,
and the appliance's dump and restore, which reach the backend over one -- so
they are stated here, once, below all of them.
"""

from __future__ import annotations

from pathlib import Path

#: The bundle's file names, shared by the writer and the environment that
#: names them.
CA_FILE = 'ca.crt'
CERT_FILE = 'client.crt'
KEY_FILE = 'client.key'
#: The file inside a client bundle that names the backend.
URL_FILE = 'backend-url'

#: The libpq variables that carry a bundle's three files. Standard names, read
#: by libpq itself and by the driver Pulumi's Postgres backend uses, which is
#: what lets the connection string stay free of paths.
CA_ENV = 'PGSSLROOTCERT'
CERT_ENV = 'PGSSLCERT'
KEY_ENV = 'PGSSLKEY'


def ssl_env(directory: Path) -> dict[str, str]:
    """The libpq variables naming the bundle in `directory`.

    The other half of the bundle's connection string, and the half that
    varies by machine: a client bundle is reachable from anywhere its files
    are, so where they are is said once, in the environment, by whoever knows
    — `mise.toml` for a workstation slot, a workflow step for the `ci` bundle
    it materializes, and this function for the commands that drive `pg_dump`,
    `pg_restore` and `pulumi` themselves.

    Absolute, because the tools are run with a working directory of their own.
    """
    directory = directory.resolve()
    return {
        CA_ENV: str(directory / CA_FILE),
        CERT_ENV: str(directory / CERT_FILE),
        KEY_ENV: str(directory / KEY_FILE),
    }


#: The query parameter a bundle's connection string gains on its way to
#: `pulumi`, and only there (`pulumi_url`).
PULUMI_PARAMETER = 'binary_parameters=yes'
#: The scheme of Pulumi's Postgres backend, the one whose driver reads it.
POSTGRES_SCHEME = 'postgres://'


def pulumi_url(url: str) -> str:
    """The `PULUMI_BACKEND_URL` a bundle's connection string `url` becomes.

    A `postgres://` `url` with `binary_parameters=yes` added to its query,
    and any other `url` as it is. Pulumi's Postgres backend hands its URL,
    less a `table` parameter, to the `lib/pq` driver, and this parameter
    makes the driver send each statement that carries parameters as one
    Parse, Bind, Describe, Execute and Sync, in a single round trip, instead
    of a Parse and Describe answered before the Bind and Execute go out. A pooler that lends a server for one transaction can hand
    it to another client between those two round trips, and cannot between
    messages of one. Every parameter the backend passes is a string or an
    integer, so none goes over in binary despite the name: what changes on
    the wire is the grouping alone.

    The bundle's own file keeps the URL without it, because libpq refuses
    the parameter (`invalid URI query parameter`), and `psql`, `pg_dump` and
    `pg_restore` read that file (`kluster.lib.state_backend.state`).
    `mise.toml` forms the same value from the same file for every run it
    starts, so the two spellings are held equal by
    `tests/test_mise_env.py`. The parameter is that driver's, so a URL of
    another scheme is left alone: a `file://` backend refuses a query
    parameter it does not know, and an empty `url` names no backend.
    """
    if not url.startswith(POSTGRES_SCHEME):
        return url
    return f'{url}{"&" if "?" in url else "?"}{PULUMI_PARAMETER}'


def backend_url_file(bundle_dir: Path) -> Path | None:
    """The bundle's URL file, or None when `bundle_dir` holds no bundle.

    The directory given is the only one looked in: the default is the
    workstation slot (`kluster.scripts.credentials.workstation`), and no
    location outside the checkout stands in for it.
    """
    current = bundle_dir / URL_FILE
    return current if current.is_file() else None
