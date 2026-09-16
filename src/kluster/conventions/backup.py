"""Backups: the retention classes an app picks from, and the layout of the bucket."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass


def max_age(period: dt.timedelta) -> dt.timedelta:
    """How old the newest object of a scheduled backup may be before the backup is stale.

    One and a half periods: a run that is merely late -- a slow dump, a
    retried upload -- is still inside it, and a run that was missed is half a
    period overdue by the time it runs out. The one rule for every cadence, so
    a freshness probe and a retention class's threshold cannot disagree about
    what stale means.
    """
    return period * 3 / 2


#: The units the alert rules' duration syntax reads, largest first, each as
#: the length it stands for.
DURATION_UNITS = (
    ('w', dt.timedelta(weeks=1)),
    ('d', dt.timedelta(days=1)),
    ('h', dt.timedelta(hours=1)),
    ('m', dt.timedelta(minutes=1)),
    ('s', dt.timedelta(seconds=1)),
)


def duration(delta: dt.timedelta) -> str:
    """A length of time in the alert rules' duration syntax: one count, one unit.

    The largest unit that divides it exactly, so the same length always reads
    the same way and no length is rounded: a day and a half is `36h`, and ten
    and a half days is `252h`. The syntax has no fractions, which is why the
    unit is chosen by divisibility rather than by size.
    """
    for unit, length in DURATION_UNITS:
        if delta % length == dt.timedelta(0):
            return f'{delta // length}{unit}'
    raise ValueError(f'{delta} is not a whole number of seconds')


@dataclass(frozen=True)
class RetentionClass:
    """A backup retention policy, shared by VolSync/restic and CNPG/barman.

    An app picks a class; nobody writes retain counts or cron lines inline.
    Changing a class is one diff that previews across every affected app.
    """

    name: str
    schedule: str
    """Cron expression for the recurring backup."""
    period: dt.timedelta
    """How often `schedule` fires: the cadence `max_age` is derived from.

    Two spellings of one cadence, as `settings.DUMP_SCHEDULE` and
    `settings.DUMP_PERIOD` are for the appliance's dump: the scheduler reads
    the cron line, the freshness threshold reads the period, and a test holds
    the shape of the one to the other.
    """
    hourly: int | None = None
    daily: int | None = None
    weekly: int | None = None
    monthly: int | None = None

    @property
    def max_age(self) -> str:
        """Freshness threshold for the central vmalert rule family, in its duration syntax.

        Derived, never written: `max_age` over `period` is the one rule for
        stale, and a class carrying a threshold of its own would be the one
        row on which the rule was not one.
        """
        # The module's `max_age`: a method body resolves the bare name at
        # module scope, not on the class.
        return duration(max_age(self.period))


#: Daily, a month deep — the default every stateful app gets.
STANDARD = RetentionClass(name='standard', schedule='0 3 * * *', period=dt.timedelta(days=1), daily=30)
#: Irreplaceable data: a month of dailies plus a year of monthlies.
PRECIOUS = RetentionClass(name='precious', schedule='0 3 * * *', period=dt.timedelta(days=1), daily=30, monthly=12)
#: Large and slow-changing; weekly is enough and cheaper to store. The rule
#: gives its threshold as ten and a half days (`252h`): a missed Sunday run is
#: reported on the Wednesday after, not on the Tuesday a hand-picked nine days
#: would have.
BULKY = RetentionClass(name='bulky', schedule='0 4 * * 0', period=dt.timedelta(weeks=1), weekly=4)

RETENTION_CLASSES = (STANDARD, PRECIOUS, BULKY)


#: One bucket layout, so the backup inventory is derivable from the program.
def volsync_repo_path(namespace: str, pvc: str) -> str:
    return f'volsync/{namespace}/{pvc}'


def barman_repo_path(namespace: str, cluster: str) -> str:
    return f'cnpg/{namespace}/{cluster}'


ETCD_SNAPSHOT_PREFIX = 'etcd'
STATE_DUMP_PREFIX = 'pulumi-state'

#: The one object bucket, and it is not on the provider whose loss it insures
#: (storage.md §4): a backup kept at that provider is not a backup. The name is
#: explicit because a B2 bucket name is global and the bucket is addressed from
#: outside this program.
BUCKET_BACKUP = 'kluster-backup'

#: How long the backup bucket keeps a prior version before its lifecycle rule
#: removes it. Nothing in automation holds a delete capability, so a restic or
#: barman deletion degrades to a hide — and this is how long that hide has to
#: be recoverable before it ages out (storage.md §4).
BACKUP_VERSION_RETENTION_DAYS = 30
