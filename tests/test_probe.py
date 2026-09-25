"""`state-backend probe`: the two scheduled checks, their thresholds, and what the run answers with.

Both probes are driven through their seams -- a handshake that answers with a
transcript shaped like `openssl s_client -showcerts` prints one, and the fake
B2 account with a key minted through it -- under a clock the case pins, so a
verdict is a function of the certificate's dates, the newest dump's stamp and
`now`, and never of how long the case took (framework/testing.md §7 item 5).

The thresholds are held as relations: the alert margin below the renewal
margin, the dump age from the one rule every scheduled backup follows, the
timer's calendar form agreeing with the period the probe reads.
"""

from __future__ import annotations

import argparse
import datetime as dt
import logging
import os
import re
import subprocess as sp
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast
from urllib.parse import unquote

import pytest
import requests
from b2_api import FakeApi
from memory_kit import MemoryKit
from state_dump_box import Box

from kluster import conventions
from kluster.conventions import backup
from kluster.scripts.credentials import b2, entries, masters, pki
from kluster.scripts.credentials.kdbx import KdbxStore
from kluster.scripts.credentials.masters import CredentialRejected
from kluster.scripts.credentials.pulumi_config import SlotRefused
from kluster.scripts.state_backend import cli, config, probe, settings, state

UTC = dt.timezone.utc
NOW = dt.datetime(2026, 9, 16, 6, 23, tzinfo=UTC)
SEED_ENTRY = entries.SEEDS['b2'].entry
PREFIX = b2.DUMP_PREFIX


# -- the thresholds -----------------------------------------------------------


def test_the_alert_margin_opens_after_the_renewal_margin() -> None:
    # The probe is the backstop for a box nobody has converged: a backstop
    # that fires before the converge would have reported is the first
    # reporter rather than the last.
    assert config.EXPIRY_ALERT_MARGIN < config.RENEWAL_MARGIN
    assert config.EXPIRY_ALERT_MARGIN > dt.timedelta(0)


def test_the_dump_age_follows_the_one_rule_for_stale() -> None:
    assert settings.DUMP_MAX_AGE == backup.max_age(settings.DUMP_PERIOD)


def test_the_timer_s_calendar_form_says_what_the_period_says() -> None:
    # Two spellings of one cadence: the systemd calendar expression the
    # timer reads, and the period the probe measures against. A daily
    # expression names every day and one time of day, and nothing else does.
    daily = re.fullmatch(r'\*-\*-\* \d\d:\d\d:\d\d', settings.DUMP_SCHEDULE) is not None

    assert daily == (settings.DUMP_PERIOD == dt.timedelta(days=1))


# -- the certificate probe ----------------------------------------------------


@pytest.fixture(scope='module')
def authority() -> pki.Authority:
    return pki.Authority.from_pem(pki.generate_ca_key())


def _transcript(*certificates: bytes) -> str:
    """What `openssl s_client -showcerts` prints for a chain: the leaf first, each block after its index line."""
    chain = ''.join(f' {index} s:CN=x\n   i:CN=y\n{pem.decode()}' for index, pem in enumerate(certificates))
    return f'CONNECTED(00000003)\n---\nCertificate chain\n{chain}---\nServer certificate\nsubject=CN=x\n'


class Handshake:
    """A handshake that answers with what a case put there, and remembers how it was asked."""

    def __init__(self, stdout: str = '', returncode: int = 0, stderr: str = '', hangs: bool = False) -> None:
        self.stdout: str = stdout
        self.returncode: int = returncode
        self.stderr: str = stderr
        self.hangs: bool = hangs
        self.argv: list[str] | None = None

    def __call__(self, argv: Sequence[str]) -> sp.CompletedProcess[str]:
        self.argv = list(argv)
        if self.hangs:
            raise sp.TimeoutExpired(cmd=self.argv, timeout=probe.HANDSHAKE_TIMEOUT)
        return sp.CompletedProcess(args=self.argv, returncode=self.returncode, stdout=self.stdout, stderr=self.stderr)


def _serving(authority: pki.Authority, *, issued: dt.datetime, address: str = settings.ADDRESS) -> Handshake:
    """A box serving a leaf issued at `issued`, with the CA after it in the chain."""
    leaf = authority.issue_server(address, now=issued)
    return Handshake(_transcript(leaf.cert_pem, authority.certificate(now=issued).cert_pem))


def _left(days: int) -> dt.datetime:
    """The issuance that leaves a leaf `days` of validity at `NOW`."""
    return NOW - pki.LEAF_VALIDITY + dt.timedelta(days=days)


def test_the_handshake_is_the_one_provision_makes_with_the_chain_printed() -> None:
    handshake = Handshake()

    _ = probe.certificate(now=NOW, handshake=handshake)

    # `-starttls postgres` because the port speaks Postgres before TLS, and
    # `-showcerts` because the leaf is read off the transcript.
    assert handshake.argv == [
        'openssl',
        's_client',
        '-connect',
        f'{settings.ADDRESS}:{settings.PORT}',
        '-starttls',
        'postgres',
        '-showcerts',
    ]


def test_the_probe_s_own_handshake_closes_its_stdin_and_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    def fake_run(argv: list[str], **kwargs: object) -> Any:
        seen.update(kwargs)
        return sp.CompletedProcess(args=argv, returncode=0, stdout='', stderr='')

    monkeypatch.setattr(probe.sp, 'run', fake_run)

    _ = probe.s_client(['openssl'])

    # Otherwise a successful handshake hangs on an inherited terminal, and
    # is reported as no answer once the bound runs out.
    assert seen['stdin'] is sp.DEVNULL
    assert seen['timeout'] == probe.HANDSHAKE_TIMEOUT


def test_the_leaf_is_the_first_certificate_of_the_chain(authority: pki.Authority) -> None:
    leaf = authority.issue_server(settings.ADDRESS, now=NOW)
    ca = authority.certificate(now=NOW)

    read = probe.leaf_certificate(_transcript(leaf.cert_pem, ca.cert_pem))

    # The CA is sent after the leaf and has years left; a probe reading the
    # wrong block would never alert on the certificate that expires first.
    assert read is not None
    assert read.subject.rfc4514_string() == f'CN={settings.ADDRESS}'
    assert probe.leaf_certificate('CONNECTED\n---\nno certificate here\n') is None


def test_a_certificate_with_more_than_the_margin_left_passes(authority: pki.Authority) -> None:
    verdict = probe.certificate(now=NOW, handshake=_serving(authority, issued=_left(31)))

    assert verdict.passed
    assert settings.ADDRESS in verdict.observed
    assert '31 day(s)' in verdict.observed


def test_a_certificate_inside_the_margin_fails_into_the_reissue_playbook(authority: pki.Authority) -> None:
    verdict = probe.certificate(now=NOW, handshake=_serving(authority, issued=_left(29)))

    assert not verdict.passed
    assert verdict.playbook == 'physical/state-backend.md §7.1'
    assert '29 day(s)' in verdict.observed
    assert f'{config.EXPIRY_ALERT_MARGIN.days}-day alert margin' in verdict.observed


def test_the_margin_is_read_from_config_and_not_from_a_number_of_its_own(
    authority: pki.Authority, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(config, 'EXPIRY_ALERT_MARGIN', dt.timedelta(days=40))

    assert not probe.certificate(now=NOW, handshake=_serving(authority, issued=_left(31))).passed
    assert probe.certificate(now=NOW, handshake=_serving(authority, issued=_left(41))).passed


def test_an_expired_certificate_fails_by_its_date(authority: pki.Authority) -> None:
    verdict = probe.certificate(now=NOW, handshake=_serving(authority, issued=_left(-1)))

    assert verdict.playbook == 'physical/state-backend.md §7.1'
    assert 'expired on' in verdict.observed


def test_a_certificate_not_yet_valid_fails(authority: pki.Authority) -> None:
    verdict = probe.certificate(now=NOW, handshake=_serving(authority, issued=NOW + dt.timedelta(days=1)))

    assert verdict.playbook == 'physical/state-backend.md §7.1'
    assert 'not valid until' in verdict.observed


def test_a_certificate_naming_another_address_fails_naming_both(authority: pki.Authority) -> None:
    # Every client connects with `sslmode=verify-full` by the address, so a
    # leaf with years left that names another one refuses them all.
    verdict = probe.certificate(now=NOW, handshake=_serving(authority, issued=_left(400), address='192.0.2.10'))

    assert verdict.playbook == 'physical/state-backend.md §7.1'
    assert '192.0.2.10' in verdict.observed
    assert settings.ADDRESS in verdict.observed


def test_a_box_that_refuses_the_handshake_is_unreachable_into_the_rebuild_playbook() -> None:
    verdict = probe.certificate(now=NOW, handshake=Handshake(returncode=1, stderr='connect:errno=111\nmore'))

    assert verdict.playbook == 'physical/state-backend.md §7.3'
    assert 'connect:errno=111' in verdict.observed


def test_a_box_that_does_not_answer_is_unreachable_into_the_rebuild_playbook() -> None:
    verdict = probe.certificate(now=NOW, handshake=Handshake(hangs=True))

    assert verdict.playbook == 'physical/state-backend.md §7.3'
    assert f'{probe.HANDSHAKE_TIMEOUT}s' in verdict.observed


def test_a_handshake_that_served_no_certificate_fails() -> None:
    verdict = probe.certificate(now=NOW, handshake=Handshake(stdout='CONNECTED\n---\n'))

    assert verdict.playbook == 'physical/state-backend.md §7.3'
    assert 'no certificate' in verdict.observed


# -- the dump-age probe -------------------------------------------------------


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> FakeApi:
    fake = FakeApi()
    monkeypatch.setattr(b2.requests, 'get', fake.get)
    monkeypatch.setattr(b2.requests, 'post', fake.post)
    monkeypatch.setattr(
        conventions, 'B2_ACCOUNT', conventions.B2Account(region='us-west-002', account_id=fake.master.key_id)
    )
    return fake


@pytest.fixture
def lister(api: FakeApi) -> tuple[b2.AppKey, str]:
    """The freshness key as the mint delivers it, and the bucket it is confined to."""
    kit: KdbxStore = MemoryKit()
    root = masters.Credential(
        root=masters.ROOTS['b2'], values={'account-id': api.master.key_id, 'key': api.master.secret}
    )
    _ = b2.create_seed(root=root, seeds=kit, seed_entry=SEED_ENTRY)
    session = b2.Session.from_entry(kit, SEED_ENTRY)
    bucket_id = b2.ensure_bucket(session, settings.B2_BUCKET, prefix=settings.B2_PREFIX, retention_days=30)
    key, _ = b2.mint_freshness_dumps_key(session, bucket_id=bucket_id).deliver(lambda key: key)
    return key, bucket_id


def _dump(taken: dt.datetime) -> str:
    """The object the appliance uploads for a dump taken at `taken`."""
    return f'{PREFIX}{taken.strftime("%Y%m%dT%H%M%SZ")}.dump.age'


def _dumps(key: b2.AppKey, *, now: dt.datetime = NOW) -> probe.Verdict:
    return probe.dumps(key.key_id, key.key, now=now)


def test_the_probe_reads_the_name_the_box_uploads_under(tmp_path: Path) -> None:
    """The seam is the box's writer: the objects under the prefix are named by the appliance's own script.

    The script is run as the box runs it, over fakes of what it calls
    (`state_dump_box`), so the name it hands the upload is the one it would
    hand B2, from its own format literal and its own clock. A stamp the
    probe could not read would make every nightly object a stranger, and no
    case holding the probe to a stamp typed here -- or to the workstation's
    writer -- would notice.
    """
    box = Box(tmp_path)
    before = dt.datetime.now(UTC).replace(microsecond=0)

    ran = box.run(env={'B2_PREFIX': conventions.STATE_DUMP_PREFIX})

    after = dt.datetime.now(UTC)
    assert ran.returncode == 0, ran.stderr
    (put,) = box.of('curl')[2:]
    sent = put.header('X-Bz-File-Name')
    assert sent is not None
    taken = probe.dumped_at(unquote(sent))
    # Read, and read as the moment the box stamped -- the bracket is the
    # call itself, so a stall widens it and decides nothing.
    assert taken is not None, sent
    assert before <= taken <= after


def test_the_operator_s_dump_carries_the_same_stamp() -> None:
    # `state-backend dump` names a hand-taken dump by the same stamp
    # (`state.dump_name`), which is what makes it interchangeable with a
    # nightly one on disk; the probe never lists one, so this holds the two
    # writers to each other rather than the probe to either.
    stamp = state.dump_name(NOW).removeprefix(f'{settings.NAME}-').removesuffix('.dump.age')

    assert probe.dumped_at(f'{PREFIX}{stamp}.dump.age') == NOW


def test_a_name_that_is_not_a_dump_s_reads_as_none() -> None:
    assert probe.dumped_at(f'{PREFIX}readme.txt') is None
    assert probe.dumped_at(f'{PREFIX}20260916T062300Z.dump') is None
    assert probe.dumped_at('etcd/20260916T062300Z.dump.age') is None


def test_a_dump_younger_than_the_age_passes(api: FakeApi, lister: tuple[b2.AppKey, str]) -> None:
    key, bucket_id = lister
    api.objects[bucket_id] = [_dump(NOW - dt.timedelta(days=3)), _dump(NOW - dt.timedelta(hours=35))]

    verdict = _dumps(key)

    assert verdict.passed
    assert _dump(NOW - dt.timedelta(hours=35)) in verdict.observed
    assert '35.0 h' in verdict.observed


def test_a_dump_older_than_the_age_fails_into_the_rebuild_playbook(api: FakeApi, lister: tuple[b2.AppKey, str]) -> None:
    key, bucket_id = lister
    api.objects[bucket_id] = [_dump(NOW - dt.timedelta(hours=37))]

    verdict = _dumps(key)

    assert verdict.playbook == 'physical/state-backend.md §7.3'
    assert '37.0 h' in verdict.observed
    assert 'stopped' in verdict.observed
    # The box refusing an archive that holds no stack is one way the timer
    # stops landing objects: a box replaced and not restored reads as stale.
    assert 'holding no stack' in verdict.observed


def test_the_age_is_the_settings_and_not_a_number_of_its_own(
    api: FakeApi, lister: tuple[b2.AppKey, str], monkeypatch: pytest.MonkeyPatch
) -> None:
    key, bucket_id = lister
    api.objects[bucket_id] = [_dump(NOW - dt.timedelta(hours=37))]

    assert not _dumps(key).passed
    monkeypatch.setattr(settings, 'DUMP_MAX_AGE', dt.timedelta(hours=48))
    assert _dumps(key).passed


def test_an_empty_prefix_is_its_own_failure(api: FakeApi, lister: tuple[b2.AppKey, str]) -> None:
    key, bucket_id = lister
    api.objects[bucket_id] = ['etcd/snapshot']

    verdict = _dumps(key)

    # No dump holding state has landed: the box refuses to upload one that
    # holds no stack, or its timer never fired. Neither is a dump that
    # stopped, and the message says which prefix is empty and names both.
    assert verdict.playbook == 'physical/state-backend.md §7.3'
    assert 'no object under' in verdict.observed
    assert PREFIX in verdict.observed
    assert 'holds no stack' in verdict.observed
    assert 'never fired' in verdict.observed


def test_an_object_not_named_like_a_dump_fails_by_name(api: FakeApi, lister: tuple[b2.AppKey, str]) -> None:
    key, bucket_id = lister
    api.objects[bucket_id] = [_dump(NOW - dt.timedelta(hours=1)), f'{PREFIX}notes.txt']

    verdict = _dumps(key)

    assert verdict.playbook == 'physical/state-backend.md §7.3'
    assert f'{PREFIX}notes.txt' in verdict.observed


def test_the_newest_dump_is_found_past_the_first_page(api: FakeApi, lister: tuple[b2.AppKey, str]) -> None:
    key, bucket_id = lister
    api.objects[bucket_id] = [_dump(NOW - dt.timedelta(days=days)) for days in range(6, 0, -1)]
    api.file_page_limit = 2

    verdict = _dumps(key)

    # B2 pages the listing at a size it chooses and names sort oldest first,
    # so a probe that read one page would call a six-day-old dump the newest.
    assert verdict.passed
    assert _dump(NOW - dt.timedelta(days=1)) in verdict.observed


def test_the_listing_is_served_to_the_list_only_key_over_the_prefix(
    api: FakeApi, lister: tuple[b2.AppKey, str]
) -> None:
    key, bucket_id = lister
    api.objects[bucket_id] = [_dump(NOW)]
    api.listings.clear()

    _ = _dumps(key)

    assert api.listings == [(key.key_id, bucket_id, PREFIX)]


def test_a_key_b2_refuses_is_the_dump_probe_s_verdict_naming_no_key_id(api: FakeApi) -> None:
    """A probe that could not run failed, and says so in its own verdict.

    As an exception it would end the run before the other probe's verdict was
    counted. And the verdict names the two variables rather than the key id
    B2's refusal carries: in the workflow the id is a secret, and a job
    output holding one arrives empty.
    """
    rejected = 'no-such-key'
    with pytest.raises(CredentialRejected, match=rejected):
        _ = b2.Session.authorize_confined(rejected, 'nor-secret')

    verdict = probe.dumps(rejected, 'nor-secret', now=NOW)

    assert not verdict.passed
    assert verdict.playbook == 'credentials.md §4'
    assert probe.KEY_ID_ENV in verdict.observed and 'credentials derived b2-freshness-dumps mint' in verdict.observed
    assert rejected not in str(verdict)


class _Listing:
    """A session whose listing ends the way a case says, for a key the authorization accepted."""

    def __init__(self, failure: Exception) -> None:
        self.failure: Exception = failure

    def file_names(self, _bucket_id: str, *, prefix: str) -> tuple[str, ...]:
        raise self.failure


def _answered(status: int) -> requests.HTTPError:
    """What `raise_for_status` raises for a B2 answer of `status`."""
    response = requests.Response()
    response.status_code = status
    return requests.HTTPError(f'{status} from B2', response=response)


def _authorizing_to(failure: Exception) -> probe.Authorize:
    def authorize(_key_id: str, _key: str) -> tuple[b2.Session, str]:
        return cast('b2.Session', _Listing(failure)), 'bucket-id'

    return authorize


def _not_authorizing(failure: Exception) -> probe.Authorize:
    def authorize(_key_id: str, _key: str) -> tuple[b2.Session, str]:
        raise failure

    return authorize


@pytest.mark.parametrize('status', [401, 403])
def test_a_key_b2_refuses_at_the_listing_is_the_refused_key_s_verdict(status: int) -> None:
    # A key that authorizes and is then refused the prefix -- re-scoped, or
    # retired between the two calls -- is the same repair as one refused at
    # the door.
    verdict = probe.dumps('id', 'secret', now=NOW, authorize=_authorizing_to(_answered(status)))

    assert verdict.playbook == 'credentials.md §4'


B2_SILENT = {
    'no connection at the authorization': _not_authorizing(requests.ConnectionError('proxy refused')),
    'a timeout at the authorization': _not_authorizing(requests.Timeout('read timed out')),
    'a server error at the listing': _authorizing_to(_answered(503)),
}


@pytest.mark.parametrize('authorize', list(B2_SILENT.values()), ids=list(B2_SILENT))
def test_b2_not_answering_is_the_dump_probe_s_verdict(authorize: probe.Authorize) -> None:
    """An outage of B2's is a finding about this run, not about the dumps, and not a traceback.

    It points at §6 rather than at a playbook for the box: nothing is known
    about the dumps either way, and the next scheduled run is the retry.
    """
    verdict = probe.dumps('id', 'secret', now=NOW, authorize=authorize)

    assert not verdict.passed
    assert verdict.playbook == 'physical/state-backend.md §6'
    assert 'B2 did not answer' in verdict.observed


# -- the credential ------------------------------------------------------------


def test_the_key_is_read_from_the_two_variables_the_ops_repository_fills() -> None:
    assert probe.credential({probe.KEY_ID_ENV: 'id', probe.KEY_ENV: 'secret'}) == ('id', 'secret')


@pytest.mark.parametrize(
    ('environ', 'named'),
    [
        ({}, f'{probe.KEY_ID_ENV} and {probe.KEY_ENV} are empty'),
        ({probe.KEY_ID_ENV: 'id'}, f'{probe.KEY_ENV} is empty'),
        ({probe.KEY_ENV: 'secret', probe.KEY_ID_ENV: ''}, f'{probe.KEY_ID_ENV} is empty'),
    ],
    ids=['both', 'key', 'id'],
)
def test_an_empty_slot_is_refused_naming_the_variable_and_the_mint(environ: dict[str, str], named: str) -> None:
    with pytest.raises(SlotRefused, match=re.escape(named)) as refusal:
        _ = probe.credential(environ)

    assert 'credentials derived b2-freshness-dumps mint' in str(refusal.value)


# -- the run --------------------------------------------------------------------


def _authorize(api: FakeApi, lister: tuple[b2.AppKey, str], *, dumps_taken: dt.datetime) -> dict[str, str]:
    key, bucket_id = lister
    api.objects[bucket_id] = [_dump(dumps_taken)]
    return {probe.KEY_ID_ENV: key.key_id, probe.KEY_ENV: key.key}


@pytest.mark.parametrize(
    ('left', 'dumped_ago', 'status'),
    [
        (31, 35, 0),
        (29, 35, probe.FAILED[probe.CERTIFICATE]),
        (31, 37, probe.FAILED[probe.DUMPS]),
        (29, 37, probe.FAILED[probe.CERTIFICATE] + probe.FAILED[probe.DUMPS]),
    ],
    ids=['both-pass', 'certificate-fails', 'dumps-fail', 'both-fail'],
)
def test_the_status_says_which_probe_failed(
    authority: pki.Authority,
    api: FakeApi,
    lister: tuple[b2.AppKey, str],
    left: int,
    dumped_ago: int,
    status: int,
    caplog: pytest.LogCaptureFixture,
) -> None:
    environ = _authorize(api, lister, dumps_taken=NOW - dt.timedelta(hours=dumped_ago))
    caplog.set_level(logging.INFO, logger=probe.__name__)

    answered = probe.run(environ=environ, now=NOW, handshake=_serving(authority, issued=_left(left)))

    # One bit per probe, so a run where both failed is told apart from
    # either, and every verdict is printed whatever the earlier one found.
    assert answered == status
    assert 'certificate:' in caplog.text
    assert 'dumps:' in caplog.text


def test_every_probe_runs_whatever_the_first_found(
    authority: pki.Authority, api: FakeApi, lister: tuple[b2.AppKey, str], caplog: pytest.LogCaptureFixture
) -> None:
    environ = _authorize(api, lister, dumps_taken=NOW - dt.timedelta(hours=1))
    caplog.set_level(logging.INFO, logger=probe.__name__)

    _ = probe.run(environ=environ, now=NOW, handshake=Handshake(returncode=1))

    # The second verdict is the one a run that stopped at the first failure
    # would leave unknown on exactly the morning both are wanted.
    assert 'certificate: FAILED' in caplog.text
    assert 'dumps: ok' in caplog.text


def test_a_key_b2_refuses_leaves_the_certificate_s_verdict_standing(
    authority: pki.Authority, api: FakeApi, caplog: pytest.LogCaptureFixture
) -> None:
    """The morning both are wanted: a certificate failing, and a key the account no longer has.

    Each is printed and each has its bit, so the status names both probes
    rather than collapsing into the refusal status that names neither.
    """
    environ = {probe.KEY_ID_ENV: 'retired-key', probe.KEY_ENV: 'its-secret'}
    caplog.set_level(logging.INFO, logger=probe.__name__)

    answered = probe.run(environ=environ, now=NOW, handshake=_serving(authority, issued=_left(29)))

    assert answered == probe.FAILED[probe.CERTIFICATE] + probe.FAILED[probe.DUMPS]
    assert 'certificate: FAILED' in caplog.text
    assert 'dumps: FAILED' in caplog.text


def test_the_status_names_both_probes_when_b2_does_not_answer(
    authority: pki.Authority, caplog: pytest.LogCaptureFixture
) -> None:
    # A certificate failing on the morning B2 is down: both verdicts, both
    # bits, rather than the could-not-probe status that names neither.
    environ = {probe.KEY_ID_ENV: 'id', probe.KEY_ENV: 'secret'}
    caplog.set_level(logging.INFO, logger=probe.__name__)
    silent = _not_authorizing(requests.ConnectionError('proxy refused'))

    answered = probe.run(environ=environ, now=NOW, handshake=_serving(authority, issued=_left(29)), authorize=silent)

    assert answered == probe.FAILED[probe.CERTIFICATE] + probe.FAILED[probe.DUMPS]
    assert 'certificate: FAILED' in caplog.text
    assert 'dumps: FAILED' in caplog.text


def test_a_verdict_is_printed_before_a_later_probe_can_raise(
    authority: pki.Authority, caplog: pytest.LogCaptureFixture
) -> None:
    # What a probe raises that is not a verdict -- a defect rather than a
    # finding -- still ends the run, and the verdict already reached is on
    # the page before it does.
    environ = {probe.KEY_ID_ENV: 'id', probe.KEY_ENV: 'secret'}
    caplog.set_level(logging.INFO, logger=probe.__name__)
    broken = _not_authorizing(RuntimeError('a defect in the probe'))

    with pytest.raises(RuntimeError):
        _ = probe.run(environ=environ, now=NOW, handshake=_serving(authority, issued=_left(29)), authorize=broken)

    assert 'certificate: FAILED' in caplog.text


def test_only_runs_the_one_probe_named_and_the_certificate_needs_no_key(
    authority: pki.Authority, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger=probe.__name__)

    answered = probe.run(only=probe.CERTIFICATE, environ={}, now=NOW, handshake=_serving(authority, issued=_left(31)))

    assert answered == 0
    assert 'dumps:' not in caplog.text


def test_only_dumps_reads_the_key_before_probing_anything(authority: pki.Authority) -> None:
    handshake = _serving(authority, issued=_left(31))

    with pytest.raises(SlotRefused):
        _ = probe.run(only=probe.DUMPS, environ={}, now=NOW, handshake=handshake)

    assert handshake.argv is None


def test_each_probe_says_what_it_is_about_to_check_before_checking(
    authority: pki.Authority, api: FakeApi, lister: tuple[b2.AppKey, str], caplog: pytest.LogCaptureFixture
) -> None:
    environ = _authorize(api, lister, dumps_taken=NOW)
    caplog.set_level(logging.INFO, logger=probe.__name__)

    _ = probe.run(environ=environ, now=NOW, handshake=_serving(authority, issued=_left(31)))

    # A network step announces itself before it runs, so a silent probe reads
    # as one that is waiting rather than one that is stuck.
    lines = [record.getMessage() for record in caplog.records]
    assert _first(lines, 'checking the server certificate') < _first(lines, 'certificate:')
    assert _first(lines, 'checking the age') < _first(lines, 'dumps:')


def _first(lines: list[str], prefix: str) -> int:
    return next(index for index, line in enumerate(lines) if line.startswith(prefix))


# -- the command ------------------------------------------------------------------


def test_the_command_answers_with_the_run_s_status_and_hands_it_the_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: dict[str, object] = {}

    def fake_run(**kwargs: object) -> int:
        seen.update(kwargs)
        return probe.FAILED[probe.DUMPS]

    monkeypatch.setattr(probe, 'run', fake_run)

    assert cli.main(['probe', '--only', probe.DUMPS]) == probe.FAILED[probe.DUMPS]
    assert seen['only'] == probe.DUMPS
    # The environment itself rather than a copy: a workflow secret arrives
    # there, and it is the only place a secret can reach this command.
    assert seen['environ'] is os.environ


def test_a_refusal_from_the_command_is_one_line_and_status_one(caplog: pytest.LogCaptureFixture) -> None:
    # An empty slot is a run that could not probe, which `main` answers the
    # way it answers every refusal: the message, and 1 -- a status no probe
    # verdict produces.
    with pytest.MonkeyPatch.context() as patch:
        patch.delenv(probe.KEY_ID_ENV, raising=False)
        patch.delenv(probe.KEY_ENV, raising=False)
        assert cli.main(['probe', '--only', probe.DUMPS]) == 1

    assert 'credentials derived b2-freshness-dumps mint' in caplog.text
    assert 1 not in probe.FAILED.values()


def test_a_probe_the_command_does_not_have_is_refused_by_the_parser(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as refusal:
        _ = cli.build_parser().parse_args(['probe', '--only', 'drill'])

    assert refusal.value.code == 2
    assert 'invalid choice' in capsys.readouterr().err
    # So no probe's bit is 2: a status the parser also answers would file an
    # interface break as that probe's alert.
    assert 2 not in probe.FAILED.values()


def test_every_probe_has_a_bit_of_its_own_that_no_other_status_shares() -> None:
    # One bit per probe, so a sum names its members; each above 2, so no bit
    # is a status the refusal (1) or the layers in front of the command (2)
    # also answer.
    assert set(probe.FAILED) == set(probe.PROBES)
    bits = list(probe.FAILED.values())
    assert len(set(bits)) == len(bits)
    assert all(bit > 2 and bit & (bit - 1) == 0 for bit in bits)


def test_the_statuses_are_published_in_help() -> None:
    # A number a workflow's log is read by has to be readable without opening
    # the source, the way `provision`'s replaced status is.
    for action in cli.build_parser()._actions:  # pyright: ignore[reportPrivateUsage]
        if isinstance(action, argparse._SubParsersAction):  # pyright: ignore[reportPrivateUsage]
            chosen = cast('argparse._SubParsersAction[argparse.ArgumentParser]', action)  # pyright: ignore[reportPrivateUsage]
            help_text = chosen.choices['probe'].format_help()
            break
    else:  # pragma: no cover - the parser grew no subcommands
        raise AssertionError('the parser grew no subcommands')

    for status in probe.FAILED.values():
        assert str(status) in help_text
    assert str(config.EXPIRY_ALERT_MARGIN.days) in help_text
