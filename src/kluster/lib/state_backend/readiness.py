"""Waiting for the appliance to answer.

A box is ready when it completes a TLS handshake on the Postgres port. That
takes an address and nothing else -- no cloud client and no credential -- so
whatever brought a box up can wait for it the same way.
"""

from __future__ import annotations

import logging
import subprocess as sp
import time

from . import settings

log = logging.getLogger(__name__)


def duration(seconds: float) -> str:
    """A span of seconds the way a progress line says it: `4m05s`, or `45s`."""
    minutes, secs = divmod(int(seconds), 60)
    return f'{minutes}m{secs:02d}s' if minutes else f'{secs}s'


def _first_line(text: str) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()
    return ''


def wait_for_backend(address: str, *, timeout: int = 900) -> bool:
    """The box is up when it answers a TLS handshake on 5432.

    First boot pulls the Postgres image and fetches age, so this is minutes,
    not seconds.
    """
    deadline = time.monotonic() + timeout
    started = time.monotonic()
    announced = 0.0
    log.info(
        'waiting for a TLS handshake on %s:%d, probing every 15s — first boot pulls the Postgres image '
        'and fetches age, so this is minutes (up to %s)',
        address,
        settings.PORT,
        duration(timeout),
    )
    reason = 'not tried yet'
    while time.monotonic() < deadline:
        try:
            probe = sp.run(
                ['openssl', 's_client', '-connect', f'{address}:{settings.PORT}', '-starttls', 'postgres', '-brief'],
                # s_client keeps the connection open reading stdin after the
                # handshake, so an inherited terminal makes a *successful*
                # probe hang until the timeout below and report itself as no
                # answer -- the wait could never finish once the port opened.
                stdin=sp.DEVNULL,
                capture_output=True,
                text=True,
                timeout=30,
            )
            answered = probe.returncode == 0
            reason = _first_line(probe.stderr) or f'openssl exited {probe.returncode}'
        except sp.TimeoutExpired:
            # Two very different things look like this, which is why the
            # reason is reported rather than swallowed: Postgres binds 5432
            # before initdb finishes and then says nothing, and a firewall on
            # the path drops the packets instead of refusing them. Treating
            # either as fatal ends the wait at the moment the box comes up.
            answered = False
            reason = 'no answer within 30s — either still starting, or the packets are being dropped'
        if answered:
            return True
        elapsed = time.monotonic() - started
        if elapsed - announced >= 60:
            log.info('still waiting after %s: %s', duration(elapsed), reason)
            announced = elapsed
        time.sleep(15)
    log.error('last attempt said: %s', reason)
    log.error(
        'the appliance may be healthy and this path blocked: `state-backend ssh` reaches it over 22, '
        'and `openssl s_client -connect %s:%d -starttls postgres -brief </dev/null` from another host '
        'separates a broken box from a broken route',
        address,
        settings.PORT,
    )
    return False
