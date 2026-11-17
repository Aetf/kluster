#!/bin/sh
# Whether a VictoriaMetrics store is restored before it starts: the first of
# its pod's two init containers, in the curl image
# (`components.monitoring.stores`). It reads the store's data directory,
# `DATA`, and the bucket under `PREFIX`, and writes its decision to
# `WORK/decision` for `restore.sh`, the second, which runs `vmrestore` on
# `restore` and nothing on `start`.
#
#   a data directory holding data          start: the store starts on it
#   an empty one, and a backup at PREFIX   restore
#   an empty one, and no backup            start: the store starts empty, the first install
#   a backup that cannot be read           this fails, and the store does not start
#
# A restore that did not finish leaves `vmrestore`'s lock file in the
# directory, which VictoriaMetrics refuses to start on; that directory is
# restored again. `vmrestore` itself refuses a backup without the marker
# `vmbackup` writes when a backup completes, so a backup cut off part way is
# one that cannot be read.
set -eu
. "${0%/*}/b2.sh"

entries=$(ls -A "$DATA")
if [ -e "$DATA/restore-in-progress" ]; then
	echo "a restore into $DATA did not finish; restoring it again"
elif [ -n "$entries" ]; then
	echo "$DATA holds data; the store starts on it"
	echo start >"$WORK/decision"
	exit 0
fi

echo "reading whether the backup bucket holds a backup under $PREFIX"
b2_authorize
held=$(b2_listed "$PREFIX")
if [ "$held" = no ]; then
	if [ -e "$DATA/restore-in-progress" ]; then
		echo "no backup under $PREFIX, where a restore into $DATA had started: the store does not start" >&2
		exit 1
	fi
	echo "no backup under $PREFIX: the store starts empty"
	echo start >"$WORK/decision"
	exit 0
fi

echo "the backup under $PREFIX is restored into $DATA"
echo restore >"$WORK/decision"
