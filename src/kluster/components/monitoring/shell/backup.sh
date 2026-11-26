#!/bin/sh
# `vmbackup` of the snapshot `snapshot-create.sh` took, in `vmbackup`'s image.
# One of the three steps of the snapshot layer of
# `components.monitoring.stores`; the layer's boundary and the day it goes are
# written there.
#
# The arguments are the `vmbackup` command, run with the snapshot's name from
# `WORK/snapshot`. Whether it succeeded is left in `WORK/backed-up` rather than
# in this container's exit, so that `snapshot-delete.sh`, which runs next,
# deletes the snapshot whether or not `vmbackup` succeeded, as `vmbackup`
# deletes a snapshot it took itself, and fails the run after that where
# `vmbackup` failed.
set -eu
umask 077

name=$(cat "$WORK/snapshot")
if "$@" "-snapshotName=$name"; then
	: >"$WORK/backed-up"
else
	echo "vmbackup of snapshot $name failed; the snapshot is deleted next, and the run fails then" >&2
fi
