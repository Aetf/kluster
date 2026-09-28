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


def backend_url_file(bundle_dir: Path) -> Path | None:
    """The bundle's URL file, or None when `bundle_dir` holds no bundle.

    The directory given is the only one looked in: the default is the
    workstation slot (`kluster.scripts.credentials.workstation`), and no
    location outside the checkout stands in for it.
    """
    current = bundle_dir / URL_FILE
    return current if current.is_file() else None
