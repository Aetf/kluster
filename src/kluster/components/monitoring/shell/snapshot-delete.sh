#!/bin/sh
# The deletion of the snapshot a store's backup copied, after `vmbackup`, in
# the curl image. One of the three steps of the snapshot layer of
# `components.monitoring.stores`; the layer's boundary and the day it goes are
# written there.
#
# The snapshot named in `WORK/snapshot` is deleted at the store at `STORE`,
# the key read from the file `KEY` and sent with the name as the url-encoded
# body of a POST, as `snapshot-create.sh` sends it. It is deleted whether or
# not `vmbackup` succeeded, which `backup.sh` left in `WORK/backed-up`, and
# the step fails after deleting it where `vmbackup` did not succeed, so a
# failed backup fails its run, and the record after it is not written.
set -eu
umask 077

name=$(cat "$WORK/snapshot")
echo "deleting snapshot $name of $STORE"
answer=$(curl -sS --fail --max-time 300 --data-urlencode "authKey@$KEY" --data-urlencode "snapshot=$name" "$STORE/snapshot/delete")
status=$(printf '%s' "$answer" | tr -d '\n' | sed -n 's/.*"status": *"\([^"]*\)".*/\1/p')
if [ "$status" != ok ]; then
	echo "$STORE did not delete snapshot $name: $answer" >&2
	exit 1
fi
echo "deleted snapshot $name"
if [ ! -e "$WORK/backed-up" ]; then
	echo "vmbackup did not back up snapshot $name, so the backup failed" >&2
	exit 1
fi
