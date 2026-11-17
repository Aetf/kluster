#!/bin/sh
# The record of a successful backup of a store that keeps every row: the last
# of its backup pod's containers, in the curl image
# (`components.monitoring.stores`), which runs only once `vmbackup` has
# succeeded. It records at `RECORD` the number of rows `backup-check.sh` read
# before the backup, which the snapshot the backup copied holds at least.
set -eu
. "${0%/*}/b2.sh"

b2_authorize
b2_put "$RECORD" "$WORK/rows"
echo "recorded $(cat "$WORK/rows") rows at $RECORD"
