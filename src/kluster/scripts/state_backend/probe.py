"""`state-backend probe`: the checks that watch the appliance from outside it.

Every probe lives outside the box (physical/state-backend.md §6), and these
are the two that run on a schedule: the server certificate, read off a TLS
handshake with no credential at all, and the age of the newest dump, read off
a listing of the dump prefix as a key that can list and nothing else. Each
answers with a `Verdict`, every verdict is printed whether it passed or not,
and a failure names the playbook that answers it -- an alert with no procedure
is the one shape architecture.md §4.3 refuses.

The thresholds are not this module's: the certificate margin is
`config.EXPIRY_ALERT_MARGIN`, the dump age is `settings.DUMP_MAX_AGE`, and
the address is `settings.ADDRESS`, so the workflow that runs this carries no
number and no host of its own.

**This is a console-script surface another repository invokes.** The ops
repository's scheduled workflow (ci.md §3) runs `state-backend probe` from a
checkout of this repository at a pinned commit, and hands it the list-only
key through the two variables `KEY_ID_ENV` and `KEY_ENV` name, from that
repository's own secrets. The command, its `--only` values, the exit statuses
below and those two names are that workflow's interface: renaming any of them
lands together with the pin bump that adopts it there.
"""

from __future__ import annotations

import datetime as dt
import ipaddress
import logging
import re
import subprocess as sp
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

from cryptography import x509

from ..credentials import b2
from ..credentials.pulumi_config import SlotRefused
from . import config, settings

log = logging.getLogger(__name__)

#: The variables the dump-age probe reads the list-only key from. A workflow
#: secret has nowhere else to come from, and the names are constants here --
#: not in the slot map -- because this is the reader: the map's row imports
#: them, so the secret the mint pushes and the one the probe reads cannot be
#: two names.
KEY_ID_ENV = 'B2_FRESHNESS_DUMPS_KEY_ID'
KEY_ENV = 'B2_FRESHNESS_DUMPS_KEY'

#: The probes, by the name `--only` selects each by.
CERTIFICATE = 'certificate'
DUMPS = 'dumps'
PROBES = (CERTIFICATE, DUMPS)

#: What a run exits with: one bit per failed probe, summed, so a caller reads
#: which failed off the status alone and a run where both failed is told apart
#: from either. 1 is not among them, because it is `cli.main`'s: a refusal --
#: no key in the environment, a key the account rejects -- is a run that could
#: not probe, which is a different fact from a probe that did and failed.
FAILED = {CERTIFICATE: 2, DUMPS: 4}

#: The playbooks a failure sends the reader to (state-backend.md §7): an
#: expiring or wrong certificate is a re-issue, and everything else -- a box
#: that does not answer, a prefix with no dump, a dump that stopped -- starts
#: from the rebuild path, which doubles as the diagnosis (§6).
REISSUE_PLAYBOOK = 'physical/state-backend.md §7.1'
REBUILD_PLAYBOOK = 'physical/state-backend.md §7.3'

#: How long one handshake may take. Wall-clock, because nothing here yields
#: to count turns, and it fails as `TimeoutExpired` naming its seconds.
HANDSHAKE_TIMEOUT = 30

#: A certificate as `openssl s_client -showcerts` prints it. The chain is
#: printed leaf first, so the first block is the server's own certificate.
PEM_CERTIFICATE = re.compile(r'-----BEGIN CERTIFICATE-----\n.*?-----END CERTIFICATE-----', re.S)

#: How the appliance names a dump (`deploy/state-backend/state-dump.sh`): the
#: prefix, a UTC stamp, and the two suffixes. The stamp is the moment the dump
#: was taken, and it is what the age is read off -- names sort the way they
#: were written, so the newest object is the last name.
STAMP = '%Y%m%dT%H%M%SZ'
DUMP_NAME = re.compile(rf'^{re.escape(b2.DUMP_PREFIX)}(?P<stamp>\d{{8}}T\d{{6}}Z)\.dump\.age$')

#: Seams for the tests: the handshake, and the authorization of the list-only
#: key. Each is one call whose answer the probe reads, so a case pins the
#: answer and the clock together rather than a network.
Handshake = Callable[[Sequence[str]], sp.CompletedProcess[str]]
Authorize = Callable[[str, str], tuple[b2.Session, str]]


@dataclass(frozen=True)
class Verdict:
    """What one probe found, and where a failure sends the reader.

    A failure is a verdict that names a playbook; a pass names none. Both
    carry what was observed, because a green run whose log says nothing about
    what it measured cannot be told from one that measured nothing.
    """

    probe: str
    observed: str
    playbook: str | None = None

    @property
    def passed(self) -> bool:
        return self.playbook is None

    def __str__(self) -> str:
        if self.playbook is None:
            return f'{self.probe}: ok — {self.observed}'
        return f'{self.probe}: FAILED — {self.observed}; playbook: {self.playbook}'


def s_client(argv: Sequence[str]) -> sp.CompletedProcess[str]:
    """One `openssl s_client` run, its stdin closed.

    Closed for the reason `provision.wait_for_backend` closes it: `s_client`
    keeps the connection open reading stdin once the handshake is done, and an
    inherited terminal would make a successful probe hang until the timeout.
    """
    return sp.run(list(argv), stdin=sp.DEVNULL, capture_output=True, text=True, timeout=HANDSHAKE_TIMEOUT)


def _first_line(text: str) -> str:
    return text.strip().splitlines()[0] if text.strip() else ''


def leaf_certificate(output: str) -> x509.Certificate | None:
    """The server's own certificate out of what `s_client -showcerts` printed, or None where it printed none."""
    match = PEM_CERTIFICATE.search(output)
    if match is None:
        return None
    return x509.load_pem_x509_certificate(match.group(0).encode())


def _names(certificate: x509.Certificate) -> list[str]:
    """The IP addresses a certificate's SAN carries, as text; empty where it carries none."""
    try:
        san = certificate.extensions.get_extension_for_class(x509.SubjectAlternativeName)
    except x509.ExtensionNotFound:
        return []
    return [str(address) for address in san.value.get_values_for_type(x509.IPAddress)]


def certificate(
    *,
    address: str | None = None,
    port: int = settings.PORT,
    now: dt.datetime | None = None,
    handshake: Handshake = s_client,
) -> Verdict:
    """The server-leaf certificate the box serves: valid, not about to expire, and naming the box.

    The leaf and only it: the CA is not what a handshake proves, and the two
    client certificates are held by their consumers, whose first `pulumi`
    command is their expiry's report (state-backend.md §6). Credential-free,
    because the box sends its certificate before anything authenticates and
    the handshake completes without a client certificate: what needs one is
    the connection after it.
    """
    address = address or settings.ADDRESS
    argv = ['openssl', 's_client', '-connect', f'{address}:{port}', '-starttls', 'postgres', '-showcerts']
    log.info('checking the server certificate %s:%d serves, over one TLS handshake and no credential', address, port)
    try:
        completed = handshake(argv)
    except sp.TimeoutExpired:
        return Verdict(
            CERTIFICATE,
            f'no answer from {address}:{port} within {HANDSHAKE_TIMEOUT}s — the box is down, or the packets are '
            'being dropped on the way',
            REBUILD_PLAYBOOK,
        )
    if completed.returncode != 0:
        why = _first_line(completed.stderr) or f'openssl exited {completed.returncode}'
        return Verdict(CERTIFICATE, f'{address}:{port} did not complete a TLS handshake: {why}', REBUILD_PLAYBOOK)
    leaf = leaf_certificate(completed.stdout)
    if leaf is None:
        return Verdict(
            CERTIFICATE, f'{address}:{port} completed a handshake and served no certificate', REBUILD_PLAYBOOK
        )

    moment = now or dt.datetime.now(dt.timezone.utc)
    expiry = leaf.not_valid_after_utc
    remaining = expiry - moment
    if moment < leaf.not_valid_before_utc:
        return Verdict(
            CERTIFICATE,
            f'the server certificate is not valid until {leaf.not_valid_before_utc.isoformat()}',
            REISSUE_PLAYBOOK,
        )
    if remaining < dt.timedelta(0):
        return Verdict(CERTIFICATE, f'the server certificate expired on {expiry.date().isoformat()}', REISSUE_PLAYBOOK)
    if remaining < config.EXPIRY_ALERT_MARGIN:
        return Verdict(
            CERTIFICATE,
            f'the server certificate expires on {expiry.date().isoformat()}, {remaining.days} day(s) from now and '
            f'inside the {config.EXPIRY_ALERT_MARGIN.days}-day alert margin',
            REISSUE_PLAYBOOK,
        )
    named = _names(leaf)
    if str(ipaddress.ip_address(address)) not in named:
        listed = ', '.join(named) or 'no address'
        return Verdict(
            CERTIFICATE,
            f'the server certificate names {listed} and not {address}, so every verify-full client refuses it',
            REISSUE_PLAYBOOK,
        )
    return Verdict(
        CERTIFICATE,
        f'the server certificate names {address} and expires on {expiry.date().isoformat()}, '
        f'{remaining.days} day(s) from now',
    )


def dumped_at(name: str) -> dt.datetime | None:
    """When the dump an object is named for was taken, or None for a name that is not a dump's."""
    match = DUMP_NAME.match(name)
    if match is None:
        return None
    return dt.datetime.strptime(match.group('stamp'), STAMP).replace(tzinfo=dt.timezone.utc)


def hours(span: dt.timedelta) -> str:
    """A span as the hours a reader compares a dump's age in."""
    return f'{span.total_seconds() / 3600:.1f} h'


def credential(environ: Mapping[str, str]) -> tuple[str, str]:
    """The list-only key out of the environment, or a refusal naming what fills it."""
    key_id, key = environ.get(KEY_ID_ENV, ''), environ.get(KEY_ENV, '')
    missing = [name for name, value in ((KEY_ID_ENV, key_id), (KEY_ENV, key)) if not value]
    if missing:
        raise SlotRefused(
            f'the dump-age probe reads the list-only B2 key from {KEY_ID_ENV} and {KEY_ENV}, and '
            f'{" and ".join(missing)} {"is" if len(missing) == 1 else "are"} empty; '
            '`credentials derived b2-freshness-dumps mint` fills the secrets a workflow hands over as those'
        )
    return key_id, key


def dumps(
    key_id: str,
    key: str,
    *,
    now: dt.datetime | None = None,
    max_age: dt.timedelta | None = None,
    authorize: Authorize = b2.Session.authorize_confined,
) -> Verdict:
    """The newest object under the dump prefix is younger than `max_age`.

    An empty prefix is its own failure rather than a stale one: it is a box
    rebuilt and never restored, or a timer that never fired, and neither is a
    dump that stopped. An object under the prefix that is not named like a
    dump fails too, by name: nothing but the appliance's uploader can write
    there, so a stranger is a fact the operator has to see.
    """
    max_age = max_age or settings.DUMP_MAX_AGE
    log.info('checking the age of the newest dump under %s, as the list-only key', b2.DUMP_PREFIX)
    session, bucket_id = authorize(key_id, key)
    names = session.file_names(bucket_id, prefix=b2.DUMP_PREFIX)
    if not names:
        return Verdict(
            DUMPS,
            f'no object under {b2.DUMP_PREFIX} — a box rebuilt and never restored, or a dump timer that never fired',
            REBUILD_PLAYBOOK,
        )
    taken_at: dict[str, dt.datetime] = {}
    strangers: list[str] = []
    for name in names:
        taken = dumped_at(name)
        if taken is None:
            strangers.append(name)
        else:
            taken_at[name] = taken
    if strangers:
        return Verdict(
            DUMPS,
            f'{len(strangers)} object(s) under {b2.DUMP_PREFIX} are not named like a dump, the first being '
            f'{strangers[0]!r} — nothing but the appliance uploads there, so something else has',
            REBUILD_PLAYBOOK,
        )
    newest = max(taken_at, key=taken_at.__getitem__)
    age = (now or dt.datetime.now(dt.timezone.utc)) - taken_at[newest]
    if age > max_age:
        return Verdict(
            DUMPS,
            f'the newest dump, {newest}, was taken {hours(age)} ago, and {hours(max_age)} is the most a '
            'backup on this timer may be behind — the timer has stopped landing objects',
            REBUILD_PLAYBOOK,
        )
    return Verdict(DUMPS, f'the newest dump, {newest}, was taken {hours(age)} ago (allowed: {hours(max_age)})')


def run(
    *,
    only: str | None = None,
    environ: Mapping[str, str],
    now: dt.datetime | None = None,
    handshake: Handshake = s_client,
    authorize: Authorize = b2.Session.authorize_confined,
) -> int:
    """Run every probe, or the one `only` names, print each verdict, and answer with the exit status.

    Every chosen probe runs whatever the earlier ones found: a run that
    stopped at the first failure would leave the second probe's verdict
    unknown on exactly the morning both are wanted. The credential is read
    before any probe runs, so an empty slot is refused with its repair rather
    than after a handshake it has no bearing on.
    """
    chosen = PROBES if only is None else (only,)
    key = credential(environ) if DUMPS in chosen else None

    verdicts: list[Verdict] = []
    if CERTIFICATE in chosen:
        verdicts.append(certificate(now=now, handshake=handshake))
    if key is not None:
        verdicts.append(dumps(*key, now=now, authorize=authorize))

    for verdict in verdicts:
        if verdict.passed:
            log.info('%s', verdict)
        else:
            log.error('%s', verdict)
    failed = [verdict for verdict in verdicts if not verdict.passed]
    if failed:
        log.error('%d of %d probe(s) failed', len(failed), len(verdicts))
    return sum(FAILED[verdict.probe] for verdict in failed)
