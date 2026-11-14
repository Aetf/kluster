"""Backups: the retention classes an app picks from, and the layout of the bucket."""

from __future__ import annotations

import datetime as dt
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType


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

#: The backup bucket, and it is not on the provider whose loss it insures
#: (storage.md §4): a backup kept at that provider is not a backup. The name is
#: explicit because a B2 bucket name is global and the bucket is addressed from
#: outside this program.
BUCKET_BACKUP = 'aetf-kluster-backup'

#: How long the backup bucket keeps a prior version before its lifecycle rule
#: removes it. Nothing in automation holds a delete capability, so a restic or
#: barman deletion degrades to a hide — and this is how long that hide has to
#: be recoverable before it ages out (storage.md §4).
BACKUP_VERSION_RETENTION_DAYS = 30


class BackupFamily(StrEnum):
    """What fills a namespace's prefix of the bucket, one writer key each.

    A family's value is the word its prefix of the bucket and its key's scope
    begin with, so one namespace's keys never share a prefix (storage.md §4).
    """

    VOLSYNC = 'volsync'
    """The restic repositories of the namespace's claims under a retention
    class, at `volsync_repo_path`."""
    BARMAN = 'cnpg'
    """The archives of the namespace's databases, at `barman_repo_path`."""


def writer_scope(namespace: str, family: BackupFamily) -> str:
    """The name of one namespace's writer key of one family on the bucket.

    The scope name `physical` mints the key under and exports it by, which is
    how `credentials` finds the key it seals for the namespace.
    """
    return f'{family.value}-{namespace}'


#: Every namespace something backs up into the bucket, with the families that
#: fill its prefix: a namespace with a claim under a retention class backs up
#: through VolSync, one with a database through barman, and a namespace with
#: neither is not here (rfc-008 §4.2). Three programs agree on it: `physical`
#: declares each family's writer key from it, `credentials` seals those keys
#: into the `apps` stack's configuration (`conventions.sealed.WRITER_KEYS`),
#: and `apps` declares the claims and databases that use them. It names every
#: namespace the waves back up from the start, so that the `physical` apply
#: after it mints every key the migration needs.
#:
#: A namespace is named after the application it holds, except where a ported
#: ciphertext fixes it: a legacy SealedSecret is sealed namespace-wide, and
#: opens only in the namespace it was sealed to (declarative/workloads.md §5).
BACKED_NAMESPACES: Mapping[str, frozenset[BackupFamily]] = MappingProxyType(
    {
        'serving-system': frozenset({BackupFamily.VOLSYNC}),
        'splitpro': frozenset({BackupFamily.BARMAN}),
        'matrix': frozenset({BackupFamily.VOLSYNC}),
        'syncthing': frozenset({BackupFamily.VOLSYNC}),
        'webdav': frozenset({BackupFamily.VOLSYNC}),
        'golinks': frozenset({BackupFamily.VOLSYNC}),
        'emailproxy': frozenset({BackupFamily.VOLSYNC}),
        'spoolman': frozenset({BackupFamily.VOLSYNC}),
        'media': frozenset({BackupFamily.VOLSYNC}),
        'immich': frozenset({BackupFamily.BARMAN}),
        'jellyfin': frozenset({BackupFamily.VOLSYNC}),
        'qbittorrent': frozenset({BackupFamily.VOLSYNC}),
    }
)
