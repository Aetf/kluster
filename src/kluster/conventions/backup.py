"""Backups: the retention classes an app picks from, and the layout of the bucket."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class RetentionClass:
    """A backup retention policy, shared by VolSync/restic and CNPG/barman.

    An app picks a class; nobody writes retain counts or cron lines inline.
    Changing a class is one diff that previews across every affected app.
    """

    name: str
    schedule: str
    """Cron expression for the recurring backup."""
    hourly: int | None = None
    daily: int | None = None
    weekly: int | None = None
    monthly: int | None = None
    max_age: str = ''
    """Freshness threshold for the central vmalert rule family."""


#: Daily, a month deep — the default every stateful app gets.
STANDARD = RetentionClass(name='standard', schedule='0 3 * * *', daily=30, max_age='36h')
#: Irreplaceable data: a month of dailies plus a year of monthlies.
PRECIOUS = RetentionClass(name='precious', schedule='0 3 * * *', daily=30, monthly=12, max_age='36h')
#: Large and slow-changing; weekly is enough and cheaper to store.
BULKY = RetentionClass(name='bulky', schedule='0 4 * * 0', weekly=4, max_age='9d')

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
