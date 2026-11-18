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

import enum
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from kluster.lib.state_backend import settings

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


class Purpose(enum.Enum):
    """What a bundle's connection string is read for, which decides the form it is read in.

    A bundle stores one connection string, and the box serves it three ways:
    `pulumi`'s sessions through the pooler's transaction entry, the libpq
    tools' queries through the same entry, and `pg_dump` and `pg_restore`
    through its session entry (physical/state-backend.md §2). The forms
    differ only in what this repository's code decides -- the session
    entry's name in `settings`, and a parameter of `pulumi`'s driver -- so
    they are formed here from the one stored string rather than stored
    beside it, and a reader names the purpose it reads for (`read`).
    """

    #: `PULUMI_BACKEND_URL`: the stored string with `binary_parameters=yes`
    #: (`pulumi_url`).
    PULUMI = 'pulumi'
    #: A libpq tool's query: the stored string as it is.
    QUERY = 'query'
    #: `pg_dump` and `pg_restore`: the stored string naming the pooler's
    #: session entry as its database (`dump_url`).
    DUMP = 'dump'


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

    The bundle's file keeps the string without it, because libpq refuses
    the parameter (`invalid URI query parameter`): a query is given the
    stored string, and `pg_dump` and `pg_restore` the session entry's form
    (`form`, as `kluster.lib.state_backend.state` hands each tool its own).
    `mise.toml` forms the same value from the same file for every run it
    starts, so the two spellings are held equal by
    `tests/test_mise_env.py`. The parameter is that driver's, so a URL of
    another scheme is left alone: a `file://` backend refuses a query
    parameter it does not know, and an empty `url` names no backend. A URL
    that carries the parameter already is in this form, and is left alone
    too.
    """
    if not url.startswith(POSTGRES_SCHEME):
        return url
    name, value = PULUMI_PARAMETER.split('=')
    if (name, value) in parse_qsl(urlsplit(url).query):
        return url
    return f'{url}{"&" if "?" in url else "?"}{PULUMI_PARAMETER}'


def dump_url(url: str) -> str:
    """The connection string `pg_dump` and `pg_restore` are given: `url` naming the pooler's session entry.

    The entry pools `operator`'s sessions one at a time, which is what keeps
    the session state the two tools set -- `pg_restore`'s empty
    `search_path`, which outlasts its transaction, among it -- from reaching
    a `pulumi` session, as the transaction entry would by lending the same
    server process to the next client. A URL of another
    scheme names no such entry and is left as it is.
    """
    if not url.startswith(POSTGRES_SCHEME):
        return url
    return urlsplit(url)._replace(path=f'/{settings.SESSION_DATABASE}').geturl()


def form(url: str, purpose: Purpose) -> str:
    """The form of a stored connection string `url` that `purpose` reads."""
    match purpose:
        case Purpose.PULUMI:
            return pulumi_url(url)
        case Purpose.QUERY:
            return url
        case Purpose.DUMP:
            return dump_url(url)


def read(bundle_dir: Path, purpose: Purpose) -> str | None:
    """The connection string of the bundle in `bundle_dir`, in the form `purpose` reads; None where it holds none.

    The reader of the stored string for code that imports `kluster`; a
    program that imports the standard library alone reads it through
    `kluster.lib.state_backend.table.Settings.client`, from a settings file
    that carries this module's file names (`state.table_settings`). A file that is there and empty is
    read as the empty string, which names no backend in any form, so a
    caller can tell the two apart.
    """
    found = backend_url_file(bundle_dir)
    if found is None:
        return None
    return form(found.read_text().strip(), purpose)


def backend_url_file(bundle_dir: Path) -> Path | None:
    """The bundle's URL file, or None when `bundle_dir` holds no bundle.

    The directory given is the only one looked in: the default is the
    workstation slot (`kluster.scripts.credentials.workstation`), and no
    location outside the checkout stands in for it.
    """
    current = bundle_dir / URL_FILE
    return current if current.is_file() else None
