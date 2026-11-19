"""The appliance's nightly dump: the timer's calendar form and the period the dump checks read are one cadence."""

from __future__ import annotations

import datetime as dt
import re

from kluster.lib.state_backend import settings


def test_the_timer_s_calendar_form_says_what_the_period_says() -> None:
    # Two spellings of one cadence: the systemd calendar expression the
    # timer reads, and the period the dump-age threshold is derived from. A
    # daily expression names every day and one time of day, and nothing else
    # does.
    daily = re.fullmatch(r'\*-\*-\* \d\d:\d\d:\d\d', settings.DUMP_SCHEDULE) is not None

    assert daily == (settings.DUMP_PERIOD == dt.timedelta(days=1))  # noqa: SIM300 -- the predicate reads 'the period is one day'
