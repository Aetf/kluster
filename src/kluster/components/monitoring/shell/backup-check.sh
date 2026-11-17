#!/bin/sh
# Whether a store that keeps every row may be backed up: the first of its
# backup pod's containers, in the curl image (`components.monitoring.stores`),
# ahead of `vmbackup`.
#
# The store's backup refuses to shrink. The number of rows the store at
# `STORE` holds is read before the backup and compared with the number the
# last successful backup recorded at `RECORD` in the bucket, beside the
# backup, and a store holding fewer is not backed up. A store whose retention
# keeps every row never holds fewer, so fewer is a store that started without
# its history, which a backup would then overwrite. The number read is left in
# `WORK/rows` for `backup-record.sh`, which records it once the backup has
# succeeded.
set -eu
. "${0%/*}/b2.sh"

echo "reading how many rows $STORE holds"
metrics=$(curl -sS --fail --max-time "$CALL_TIMEOUT" "$STORE/metrics")
rows=$(printf '%s\n' "$metrics" | awk '/^vm_rows\{type="storage\// { n += $2 } END { printf "%.0f", n }')

echo "reading the rows the last backup recorded at $RECORD"
b2_authorize
listed=$(b2_listed "$RECORD")
recorded=
if [ "$listed" = yes ]; then
	recorded=$(b2_cat "$RECORD")
	case $recorded in
	'' | *[!0-9]*)
		echo "$RECORD holds no number of rows" >&2
		exit 1
		;;
	esac
fi
if [ -n "$recorded" ] && [ "$rows" -lt "$recorded" ]; then
	echo "$STORE holds $rows rows, fewer than the $recorded the last backup recorded at $RECORD: it started without its history, and it is not backed up" >&2
	exit 1
fi
printf '%s' "$rows" >"$WORK/rows"
echo "$STORE holds $rows rows; backing it up"
