"""One run of a check of the state backend from the cluster: the server certificate the box serves, or the age of its newest dump.

**A program of its own.** The monitoring component renders this file into a
ConfigMap beside the run's settings (`components.monitoring.state_backend`),
and the `oci-sdk` image runs it with the interpreter it carries
(framework/ci.md §4). So it imports nothing of `kluster` and nothing beside
the standard library: the image holds the interpreter, the `oci` SDK and the
Debian base's `openssl`, and a test holds the imports to that. The one
external tool is `openssl`, which the certificate check runs. Every number
and name a check compares against is a field of the settings, rendered from
`kluster.lib.state_backend.settings` and `conventions`, so the program carries
none of its own.

**One check a run**: `certificate <settings>` or `dumps <settings> <key
directory>`, each the command of its own CronJob, a row of the scheduled-job
family, so a finding of one check never hides a later finding of the other
behind an alert already firing. The two rows name physical/state-backend.md
§6, which routes each finding to its playbook, and the run's log is where the
finding is read: every verdict a check reaches is printed as a line of its
own, `<check>: FAILED — <what was measured>; playbook: <document> §<section>`,
or `<check>: ok — <what was measured>` for a pass. A run exits 0 when every
verdict passed and 1 otherwise, which is what moves the CronJob's last
success, or holds it where it was for the family's `…Failed` and `…Stale`
alerts. Before each step that reaches the network a run says what it is
about to check.

**The certificate** is read off one handshake, `openssl s_client -starttls
postgres -showcerts` against the box's address, with no credential: the box
sends its certificate before anything authenticates. The leaf, the first
certificate of the chain, is read with `openssl x509`, and the leaf alone is
checked: its validity window against the alert margin, and its SAN against
the address. The CA is not what a handshake proves, so no CA is read.

**The dumps** are listed with the freshness key, which the pod mounts as a
directory of files, one per key of the sealed value: B2's v3 authorization,
which names the one bucket the key is confined to, then every page of
`b2_list_file_names` under the dump prefix. The newest dump's stamp, read
off its name, is held to the settings' greatest age. A refusal never names
the key's id or the key.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import http.client
import ipaddress
import json
import logging
import re
import subprocess as sp
import sys
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

__all__ = (
    'AUTHORIZE_URL',
    'B2_TIMEOUT',
    'CERTIFICATE',
    'DUMPS',
    'HANDSHAKE_TIMEOUT',
    'KEY_FILE',
    'KEY_ID_FILE',
    'KEY_PLAYBOOK',
    'MINT',
    'MONITORING_PLAYBOOK',
    'REBUILD_PLAYBOOK',
    'REISSUE_PLAYBOOK',
    'STAMP',
    'Answer',
    'Openssl',
    'Request',
    'Settings',
    'SettingsRefused',
    'Transport',
    'Verdict',
    'certificate',
    'dumped_at',
    'dumps',
    'main',
    'openssl',
    'urllib_transport',
)

log = logging.getLogger('state-backend-check')

#: The checks, by the subcommand that runs each.
CERTIFICATE = 'certificate'
DUMPS = 'dumps'

#: The playbooks a finding sends the reader to, in the form an alert's
#: `playbook` field takes (`conventions.alert.Playbook`): an expiring or wrong
#: certificate is a re-issue, and a box that does not answer or dumps that
#: stopped start from the rebuild path, which doubles as the diagnosis.
REISSUE_PLAYBOOK = 'docs/physical/state-backend.md §7.1'
REBUILD_PLAYBOOK = 'docs/physical/state-backend.md §7.3'
#: Where a run that could not reach B2 sends the reader: the monitoring
#: section, which says whose outage that is and what retries it.
MONITORING_PLAYBOOK = 'docs/physical/state-backend.md §6'
#: Where a freshness key B2 refuses is minted again.
KEY_PLAYBOOK = 'docs/credentials.md §4'
#: The command that mints the freshness key and seals it into the cluster.
MINT = 'credentials derived b2-freshness-dumps mint'

#: How long one `openssl` run may take. Wall-clock, since nothing here yields
#: to count turns; a handshake past it is no answer.
HANDSHAKE_TIMEOUT = 30
#: How long one call to B2 may take.
B2_TIMEOUT = 60

#: Where `b2_authorize_account` answers for version 3 of B2's native API, the
#: version that authorizes a key confined to a single bucket and names that
#: bucket in its answer.
AUTHORIZE_URL = 'https://api.backblazeb2.com/b2api/v3/b2_authorize_account'
#: How many names one listing page asks for; B2 may answer with fewer.
FILE_PAGE_SIZE = 1000

#: The files of the key directory: the key's id and the key, a file per key
#: of the sealed value, named as a mover names the two halves.
KEY_ID_FILE = 'AWS_ACCESS_KEY_ID'
KEY_FILE = 'AWS_SECRET_ACCESS_KEY'

#: The stamp of a dump's name, the moment it was taken, in UTC: how the
#: appliance names a dump, `<prefix><stamp>.dump.age`. Names sort the way they
#: were written, and the age is read off the stamp.
STAMP = '%Y%m%dT%H%M%SZ'

#: A certificate as `openssl s_client -showcerts` prints it. The chain is
#: printed leaf first, so the first block is the server's own certificate.
_PEM_CERTIFICATE = re.compile(r'-----BEGIN CERTIFICATE-----\n.*?-----END CERTIFICATE-----', re.S)
#: The validity window and the IP addresses of a SAN as `openssl x509 -noout
#: -startdate -enddate -ext subjectAltName -dateopt iso_8601` prints them.
_NOT_BEFORE = re.compile(r'^notBefore=(.+)$', re.M)
_NOT_AFTER = re.compile(r'^notAfter=(.+)$', re.M)
_IP_ADDRESS = re.compile(r'IP Address:([0-9A-Fa-f:.]+)')
_X509_DATE = '%Y-%m-%d %H:%M:%SZ'

#: The errors of a call to B2 that got no answer: what `urllib` raises for a
#: connection or a TLS failure and a timeout, which are `OSError`s, and a
#: response cut off or malformed on the wire.
_UNANSWERED: tuple[type[Exception], ...] = (OSError, http.client.HTTPException)


class SettingsRefused(ValueError):
    """The run's settings are not what a run reads; the message names the field and what is wrong with it."""


@dataclass(frozen=True, kw_only=True)
class Settings:
    """What a run compares against: where the box is, the alert margin, the dump prefix and its greatest age."""

    address: str
    """The box's address, which the handshake dials and the leaf's SAN must carry."""
    port: int
    alert_margin: dt.timedelta
    """How much life the leaf must have left."""
    dump_prefix: str
    """The prefix the box uploads its dumps under, its trailing `/` included."""
    dump_max_age: dt.timedelta
    """How old the newest dump may be."""
    key_secret: str
    """The Secret the key directory is mounted from, as `<namespace>/<name>`, which a refusal names."""

    @staticmethod
    def read(path: Path) -> Settings:
        """The settings in `path`, refused by name unless each field is what a run reads."""
        try:
            raw: object = json.loads(path.read_text())
        except json.JSONDecodeError as exc:
            raise SettingsRefused(f'{path} is not JSON: {exc}') from exc
        if not isinstance(raw, dict):
            raise SettingsRefused('the settings must be a JSON object')
        fields = cast('dict[str, object]', raw)

        def text(name: str) -> str:
            value = fields.get(name)
            if not isinstance(value, str) or not value:
                raise SettingsRefused(f'the settings field {name!r} must be a non-empty string')
            return value

        def positive(name: str) -> int:
            value = fields.get(name)
            if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                raise SettingsRefused(f'the settings field {name!r} must be a positive whole number')
            return value

        return Settings(
            address=str(ipaddress.ip_address(text('address'))),
            port=positive('port'),
            alert_margin=dt.timedelta(seconds=positive('alert_margin_seconds')),
            dump_prefix=text('dump_prefix'),
            dump_max_age=dt.timedelta(seconds=positive('dump_max_age_seconds')),
            key_secret=text('key_secret'),
        )


@dataclass(frozen=True)
class Verdict:
    """What one check found, and where a failure sends the reader: a pass names no playbook.

    Both carry what was measured, because a pass whose log says nothing about
    what it measured cannot be told from one that measured nothing.
    """

    check: str
    observed: str
    playbook: str | None = None

    @property
    def passed(self) -> bool:
        return self.playbook is None

    def __str__(self) -> str:
        if self.playbook is None:
            return f'{self.check}: ok — {self.observed}'
        return f'{self.check}: FAILED — {self.observed}; playbook: {self.playbook}'


# -- The certificate ------------------------------------------------------------

#: The seam the certificate check runs `openssl` through: its arguments after
#: `openssl`, and what it reads on its standard input.
Openssl = Callable[[Sequence[str], str], sp.CompletedProcess[str]]


def openssl(argv: Sequence[str], stdin: str) -> sp.CompletedProcess[str]:
    """One `openssl` run, bounded, its standard input `stdin` and then closed.

    Closed, because `s_client` reads its standard input once the handshake is
    done and keeps the connection open while it does: the end of the input is
    what ends a handshake that succeeded.
    """
    return sp.run(['openssl', *argv], input=stdin, capture_output=True, text=True, timeout=HANDSHAKE_TIMEOUT)


@dataclass(frozen=True)
class _Leaf:
    not_before: dt.datetime
    not_after: dt.datetime
    addresses: tuple[str, ...]


def _first_line(text: str) -> str:
    return text.strip().splitlines()[0] if text.strip() else ''


def _x509_date(output: str, pattern: re.Pattern[str]) -> dt.datetime:
    match = pattern.search(output)
    if match is None:
        raise ValueError(f'`openssl x509` printed no {pattern.pattern!r}: {output!r}')
    return dt.datetime.strptime(match.group(1).strip(), _X509_DATE).replace(tzinfo=dt.UTC)


def _leaf(pem: str, run: Openssl) -> _Leaf:
    """The leaf's validity window and the IP addresses its SAN carries, as `openssl x509` reads them."""
    completed = run(['x509', '-noout', '-startdate', '-enddate', '-ext', 'subjectAltName', '-dateopt', 'iso_8601'], pem)
    if completed.returncode != 0:
        raise ValueError(f'`openssl x509` could not read the leaf: {_first_line(completed.stderr)}')
    addresses = tuple(str(ipaddress.ip_address(it)) for it in _IP_ADDRESS.findall(completed.stdout))
    return _Leaf(
        not_before=_x509_date(completed.stdout, _NOT_BEFORE),
        not_after=_x509_date(completed.stdout, _NOT_AFTER),
        addresses=addresses,
    )


def certificate(settings: Settings, *, now: dt.datetime, run: Openssl = openssl) -> list[Verdict]:
    """The leaf the box serves: valid now, with more than the alert margin left, and naming the box's address.

    A box that does not complete the handshake is one finding, and nothing
    else is known that run. Otherwise every finding on the leaf is its own
    verdict: its window, which is not yet open, closed, or closing inside the
    margin, and its SAN, which does not carry the address.
    """
    address, port = settings.address, settings.port
    log.info('checking the server certificate %s:%d serves, over one TLS handshake and no credential', address, port)
    argv = ['s_client', '-connect', f'{address}:{port}', '-starttls', 'postgres', '-showcerts']
    try:
        completed = run(argv, '')
    except sp.TimeoutExpired:
        return [
            Verdict(
                CERTIFICATE,
                f'no answer from {address}:{port} within {HANDSHAKE_TIMEOUT}s — the box is down, or the packets are '
                'being dropped on the way',
                REBUILD_PLAYBOOK,
            )
        ]
    if completed.returncode != 0:
        why = _first_line(completed.stderr) or f'openssl exited {completed.returncode}'
        return [Verdict(CERTIFICATE, f'{address}:{port} did not complete a TLS handshake: {why}', REBUILD_PLAYBOOK)]
    pem = _PEM_CERTIFICATE.search(completed.stdout)
    if pem is None:
        return [
            Verdict(CERTIFICATE, f'{address}:{port} completed a handshake and served no certificate', REBUILD_PLAYBOOK)
        ]

    leaf = _leaf(pem.group(0) + '\n', run)
    remaining = leaf.not_after - now
    expires = leaf.not_after.date().isoformat()
    found: list[Verdict] = []
    if now < leaf.not_before:
        found.append(
            Verdict(
                CERTIFICATE,
                f'the server certificate is not valid until {leaf.not_before.isoformat()}',
                REISSUE_PLAYBOOK,
            )
        )
    elif remaining < dt.timedelta(0):
        found.append(Verdict(CERTIFICATE, f'the server certificate expired on {expires}', REISSUE_PLAYBOOK))
    elif remaining < settings.alert_margin:
        found.append(
            Verdict(
                CERTIFICATE,
                f'the server certificate expires on {expires}, {remaining.days} day(s) from now and inside the '
                f'{settings.alert_margin.days}-day alert margin',
                REISSUE_PLAYBOOK,
            )
        )
    if address not in leaf.addresses:
        listed = ', '.join(leaf.addresses) or 'no address'
        found.append(
            Verdict(
                CERTIFICATE,
                f'the server certificate names {listed} and not {address}, so every verify-full client refuses it',
                REISSUE_PLAYBOOK,
            )
        )
    if found:
        return found
    return [
        Verdict(
            CERTIFICATE,
            f'the server certificate names {address} and expires on {expires}, {remaining.days} day(s) from now',
        )
    ]


# -- The dumps --------------------------------------------------------------------


@dataclass(frozen=True)
class Request:
    """One call to B2: the method, the URL, the headers and the body, `None` for a call with none."""

    method: str
    url: str
    headers: Mapping[str, str] = field(default_factory=dict[str, str])
    body: bytes | None = None


@dataclass(frozen=True)
class Answer:
    """B2's answer to a call: its status and its body."""

    status: int
    body: bytes


#: The seam every call to B2 goes through. It answers with any status B2
#: answers with, and raises one of `_UNANSWERED` where B2 did not answer.
Transport = Callable[[Request], Answer]


def urllib_transport(request: Request) -> Answer:
    """One call to B2 over `urllib`, bounded at `B2_TIMEOUT`, its status whatever B2 answered."""
    sent = urllib.request.Request(request.url, data=request.body, headers=dict(request.headers), method=request.method)
    try:
        with urllib.request.urlopen(sent, timeout=B2_TIMEOUT) as response:
            return Answer(cast('int', response.status), cast('bytes', response.read()))
    except urllib.error.HTTPError as exc:
        return Answer(exc.code, exc.read())


class _KeyRefused(Exception):
    """B2 refused the key, or the key is confined to no bucket."""


class _Unanswered(Exception):
    """B2 did not answer, or answered with an error of its own; the message is the kind, never what it carried."""


def _called(transport: Transport, request: Request) -> dict[str, object]:
    """B2's answer to `request` as a JSON object; a refusal of the key, or no answer, raised as such."""
    try:
        answer = transport(request)
    except _UNANSWERED as exc:
        raise _Unanswered(type(exc).__name__) from exc
    if answer.status in (401, 403):
        raise _KeyRefused
    if answer.status != 200:
        raise _Unanswered(f'HTTP {answer.status}')
    body: object = json.loads(answer.body)
    if not isinstance(body, dict):
        raise ValueError(f'B2 answered {request.url} with no JSON object')
    return cast('dict[str, object]', body)


def _text(body: Mapping[str, object], name: str) -> str:
    value = body.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f'B2 answered with no {name!r}')
    return value


def _object(body: Mapping[str, object], name: str) -> dict[str, object]:
    value = body.get(name)
    if not isinstance(value, dict):
        raise ValueError(f'B2 answered with no {name!r} object')
    return cast('dict[str, object]', value)


def _listing(key_id: str, key: str, prefix: str, *, transport: Transport) -> list[str]:
    """Every name under `prefix` in the bucket the key is confined to, following the pages.

    Paged at B2's choice of size, and names sort oldest first, so a listing
    that stopped at the first page would call an old dump the newest.
    """
    basic = base64.b64encode(f'{key_id}:{key}'.encode()).decode()
    log.info('authorizing the freshness key with B2')
    authorized = _called(transport, Request('GET', AUTHORIZE_URL, {'Authorization': f'Basic {basic}'}))
    storage = _object(_object(authorized, 'apiInfo'), 'storageApi')
    bucket_id = storage.get('bucketId')
    if not isinstance(bucket_id, str) or not bucket_id:
        raise _KeyRefused
    api_url = _text(storage, 'apiUrl')
    token = _text(authorized, 'authorizationToken')

    log.info('listing every object under %s', prefix)
    names: list[str] = []
    start: str | None = None
    while True:
        body: dict[str, object] = {'bucketId': bucket_id, 'prefix': prefix, 'maxFileCount': FILE_PAGE_SIZE}
        if start is not None:
            body['startFileName'] = start
        page = _called(
            transport,
            Request(
                'POST',
                f'{api_url}/b2api/v3/b2_list_file_names',
                {'Authorization': token, 'Content-Type': 'application/json'},
                json.dumps(body).encode(),
            ),
        )
        files = page.get('files')
        if not isinstance(files, list):
            raise ValueError("B2 answered a listing with no 'files'")
        names.extend(_text(cast('dict[str, object]', it), 'fileName') for it in cast('list[object]', files))
        following = page.get('nextFileName')
        if not isinstance(following, str) or not following:
            return names
        start = following


def dumped_at(name: str, prefix: str) -> dt.datetime | None:
    """When the dump an object under `prefix` is named for was taken, or None for a name that is not a dump's."""
    match = re.fullmatch(rf'{re.escape(prefix)}(\d{{8}}T\d{{6}}Z)\.dump\.age', name)
    if match is None:
        return None
    return dt.datetime.strptime(match.group(1), STAMP).replace(tzinfo=dt.UTC)


def _hours(span: dt.timedelta) -> str:
    return f'{span.total_seconds() / 3600:.1f} h'


def _key(directory: Path) -> tuple[str, str] | None:
    """The key's id and the key, out of the directory the Secret is mounted at; None where either is missing."""
    halves: list[str] = []
    for name in (KEY_ID_FILE, KEY_FILE):
        path = directory / name
        value = path.read_text().strip() if path.is_file() else ''
        if not value:
            return None
        halves.append(value)
    key_id, key = halves
    return key_id, key


def dumps(
    settings: Settings, key_directory: Path, *, now: dt.datetime, transport: Transport = urllib_transport
) -> list[Verdict]:
    """The newest dump under the prefix is younger than the greatest age, and nothing else is there.

    An object under the prefix not named like a dump is a finding of its own,
    by name: nothing but the appliance's uploader writes there. Where no
    object is named like a dump, that is a finding too, and otherwise the
    newest one's age is. A key B2 refuses, at the authorization or at the
    listing, or one confined to no bucket, is one finding, into the mint; B2
    not answering is another, into the monitoring section, and nothing about
    the dumps is known that run.
    """
    prefix = settings.dump_prefix
    refused = Verdict(
        DUMPS,
        f'B2 refused the key in the Secret {settings.key_secret}, or it is confined to no bucket, so {prefix} was not '
        f'listed — `{MINT}` mints the key again and seals it',
        KEY_PLAYBOOK,
    )
    key = _key(key_directory)
    if key is None:
        return [
            Verdict(
                DUMPS,
                f'the Secret {settings.key_secret} mounted at {key_directory} does not hold both {KEY_ID_FILE} and '
                f'{KEY_FILE} — `{MINT}` mints the key and seals it',
                KEY_PLAYBOOK,
            )
        ]
    log.info('checking the age of the newest dump under %s, as the freshness key', prefix)
    try:
        names = _listing(*key, prefix, transport=transport)
    except _KeyRefused:
        return [refused]
    except _Unanswered as exc:
        return [
            Verdict(
                DUMPS,
                f'B2 did not answer the listing ({exc}), so nothing is known about the dumps this run',
                MONITORING_PLAYBOOK,
            )
        ]

    found: list[Verdict] = []
    taken_at: dict[str, dt.datetime] = {}
    for name in names:
        taken = dumped_at(name, prefix)
        if taken is None:
            found.append(
                Verdict(
                    DUMPS,
                    f'{name!r} under {prefix} is not named like a dump — nothing but the appliance uploads there, '
                    'so something else has',
                    REBUILD_PLAYBOOK,
                )
            )
        else:
            taken_at[name] = taken
    if not taken_at:
        found.append(
            Verdict(
                DUMPS,
                f'no dump under {prefix} — no dump holding state has landed within the retention window: the box '
                'refuses to upload one that holds no stack (a box replaced and not restored, or a site before its '
                'first `pulumi stack init`), or its dump timer never fired',
                REBUILD_PLAYBOOK,
            )
        )
        return found
    newest = max(taken_at, key=taken_at.__getitem__)
    age = now - taken_at[newest]
    allowed = settings.dump_max_age
    if age > allowed:
        found.append(
            Verdict(
                DUMPS,
                f'the newest dump, {newest}, was taken {_hours(age)} ago, and {_hours(allowed)} is the most a backup '
                'on this timer may be behind — the timer has stopped landing objects: its runs fail, the refusal of '
                'an archive holding no stack (a box replaced and not restored) among the causes, or it no longer runs',
                REBUILD_PLAYBOOK,
            )
        )
    if found:
        return found
    return [Verdict(DUMPS, f'the newest dump, {newest}, was taken {_hours(age)} ago (allowed: {_hours(allowed)})')]


# -- The run ------------------------------------------------------------------------


def main(
    argv: Sequence[str] | None = None,
    *,
    now: dt.datetime | None = None,
    run: Openssl = openssl,
    transport: Transport = urllib_transport,
) -> int:
    """One check, the one the command line names, each verdict printed: 0 when every verdict passed, 1 otherwise."""
    parser = argparse.ArgumentParser(description='Check the state backend from the cluster, once.')
    checks = parser.add_subparsers(dest='check', required=True)
    certificate_parser = checks.add_parser(CERTIFICATE, help='the server certificate the box serves')
    certificate_parser.add_argument('settings', type=Path, help='the run settings, a JSON file')
    dumps_parser = checks.add_parser(DUMPS, help='the age of the newest dump')
    dumps_parser.add_argument('settings', type=Path, help='the run settings, a JSON file')
    dumps_parser.add_argument('key', type=Path, help='the directory the freshness key is mounted at')
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format='%(asctime)s %(levelname)s %(message)s', stream=sys.stderr)

    settings = Settings.read(cast('Path', args.settings))
    moment = now or dt.datetime.now(dt.UTC)
    if cast('str', args.check) == CERTIFICATE:
        verdicts = certificate(settings, now=moment, run=run)
    else:
        verdicts = dumps(settings, cast('Path', args.key), now=moment, transport=transport)
    for verdict in verdicts:
        if verdict.passed:
            log.info('%s', verdict)
        else:
            log.error('%s', verdict)
    return 0 if all(verdict.passed for verdict in verdicts) else 1


if __name__ == '__main__':
    sys.exit(main())
