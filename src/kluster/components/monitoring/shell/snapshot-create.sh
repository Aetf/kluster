#!/bin/sh
# The snapshot a store's backup copies, taken ahead of `vmbackup`, in the curl
# image. One of the three steps of the snapshot layer of
# `components.monitoring.stores`, beside `backup.sh` and
# `snapshot-delete.sh`; the layer's boundary and the day it goes are written
# there.
#
# The store at `STORE` takes the key it guards `/snapshot/*` with from the
# file `KEY`, sent as the url-encoded body of a POST, which the store reads as
# it reads a query (`CheckAuthFlag`, `lib/httpserver/httpserver.go` at
# v1.150.0). So no argument, URL or line of output carries the key. The name
# the store answers is left in `WORK/snapshot` for the steps after it.
#
# The call gives up after five minutes, `vmbackup`'s own bound on it
# (`GetHTTPClient`, `lib/snapshot/snapshot.go`), since a store can take that
# long to flush what it holds in memory into the snapshot.
set -eu
umask 077

echo "taking a snapshot of $STORE"
answer=$(curl -sS --fail --max-time 300 --data-urlencode "authKey@$KEY" "$STORE/snapshot/create")
status=$(printf '%s' "$answer" | tr -d '\n' | sed -n 's/.*"status": *"\([^"]*\)".*/\1/p')
name=$(printf '%s' "$answer" | tr -d '\n' | sed -n 's/.*"snapshot": *"\([^"]*\)".*/\1/p')
if [ "$status" != ok ] || [ -z "$name" ]; then
	echo "$STORE answered no snapshot: $answer" >&2
	exit 1
fi
# A snapshot's name is its time and a counter in hexadecimal
# (`lib/snapshot/snapshotutil`), which `vmbackup` validates in full.
case $name in
*[!0-9A-Fa-f-]*)
	echo "$STORE answered a snapshot named $name, which is no snapshot's name" >&2
	exit 1
	;;
esac
printf '%s' "$name" >"$WORK/snapshot"
echo "took snapshot $name"
