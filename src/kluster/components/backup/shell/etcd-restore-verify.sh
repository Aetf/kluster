#!/bin/sh
# The etcd restore-verify: the newest etcd snapshot in the backup bucket,
# restored into a scratch etcd that has to serve it (`components.backup.etcd`).
# It runs in the `etcd-age` image: curl's, with etcd's three binaries and `age`
# beside its busybox.
#
# A recovery hands the newest snapshot to Talos' etcd, which restores it and
# starts a member on it, so a snapshot passes when the same steps succeed on
# it. Each step catches what the ones before it do not:
#
# 1. `list`: the newest name under `PREFIX/` stamped no earlier than `MAX_AGE`
#    seconds ago, read off the bucket rather than off the snapshot job's
#    status. The stamp is fixed-width, so the listing starts at the name of
#    that moment and holds only the snapshots after it; an empty one is
#    `stale`, and a name of another form than talos-backup's is `name`.
# 2. `download`, by the id the listing answered.
# 3. `decrypt` with the identity at `IDENTITY`: the snapshot is encrypted to
#    its recipient, which proves the recipient the snapshot job reads.
# 4. `restore` with `etcdutl`, which checks etcd's own hash at the snapshot's
#    end and refuses one that does not match.
# 5. `start` an etcd on the restored directory, on loopback alone, and wait
#    until it answers healthy, for `START_BOUND` seconds at most.
# 6. `key`: the restored keyspace holds `KEY`, so it is a Kubernetes cluster's
#    and not an empty one.
#
# Each step deletes what the one before it wrote once its own output exists,
# so `WORK`, held in memory, holds at most two copies of the database. Every
# failure ends the run with a line `FAILED <step>: …` naming the object, and a
# pass with a line `ok: …` naming it, its age, and its revision and key count.
set -eu
. "${0%/*}/b2.sh"

fail() {
	echo "FAILED $1: $2" >&2
	exit 1
}

etcd_pid=
stop_etcd() {
	if [ -n "$etcd_pid" ]; then
		kill "$etcd_pid" 2>/dev/null || :
		wait "$etcd_pid" 2>/dev/null || :
	fi
}
trap stop_etcd EXIT

client=http://127.0.0.1:$CLIENT_PORT
peer=http://127.0.0.1:$PEER_PORT
now=$(date -u +%s)
since=$(date -u -d "@$((now - MAX_AGE))" +%Y-%m-%dT%H:%M:%SZ)
start="$PREFIX/$CLUSTER-$since"

echo "authorizing the reader key with the backup bucket"
b2_authorize || fail authorize "the backup bucket did not authorize the reader key"

echo "listing $PREFIX/ from $start, the snapshots of the last $MAX_AGE seconds"
newest=$(b2_newest "$PREFIX/" "$start") || fail list "the backup bucket did not list $PREFIX/"
[ -n "$newest" ] || fail stale "no snapshot under $PREFIX/ is named for $since or later, which covers none at all"
id=${newest%% *}
name=${newest#* }
stamp=${name#"$PREFIX/$CLUSTER-"}
stamp=${stamp%.snap.age}
case $name in
"$PREFIX/$CLUSTER-"[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9]T[0-9][0-9]:[0-9][0-9]:[0-9][0-9]Z.snap.age) ;;
*) fail name "$name is not named $PREFIX/$CLUSTER-<RFC 3339 UTC>.snap.age" ;;
esac
taken=$(date -u -D '%Y-%m-%dT%H:%M:%SZ' -d "$stamp" +%s)

echo "downloading $name"
b2_fetch_id "$id" "$WORK/snapshot.age" || fail download "$name did not download"

echo "decrypting $name"
age -d -i "$IDENTITY" -o "$WORK/snapshot.db" "$WORK/snapshot.age" ||
	fail decrypt "$name does not open with the identity at $IDENTITY, so it is not encrypted to the restore-verify's recipient"
rm -f "$WORK/snapshot.age"
status=$(etcdutl snapshot status "$WORK/snapshot.db" -w json) || fail restore "etcdutl cannot read $name as a snapshot"
revision=$(printf '%s' "$status" | sed -n 's/.*"revision": *\([0-9]*\).*/\1/p')
keys=$(printf '%s' "$status" | sed -n 's/.*"totalKey": *\([0-9]*\).*/\1/p')

echo "restoring $name, at revision $revision with $keys keys"
etcdutl snapshot restore "$WORK/snapshot.db" --data-dir "$WORK/etcd" --name verify \
	--initial-cluster "verify=$peer" --initial-advertise-peer-urls "$peer" >/dev/null ||
	fail restore "etcdutl did not restore $name"
rm -f "$WORK/snapshot.db"

echo "starting etcd on the restored $name, on $client, and waiting up to $START_BOUND seconds for it to answer healthy"
etcd --name verify --data-dir "$WORK/etcd" \
	--listen-client-urls "$client" --advertise-client-urls "$client" \
	--listen-peer-urls "$peer" --initial-advertise-peer-urls "$peer" \
	--initial-cluster "verify=$peer" >"$WORK/etcd.log" 2>&1 &
etcd_pid=$!
bound=$(($(date -u +%s) + START_BOUND))
until etcdctl --endpoints "$client" --command-timeout 5s endpoint health >/dev/null 2>&1; do
	if ! kill -0 "$etcd_pid" 2>/dev/null; then
		tail -n 5 "$WORK/etcd.log" >&2
		fail start "etcd exited on the restored $name"
	fi
	if [ "$(date -u +%s)" -ge "$bound" ]; then
		tail -n 5 "$WORK/etcd.log" >&2
		fail start "etcd on the restored $name did not answer healthy within $START_BOUND seconds"
	fi
	sleep 1
done

echo "reading $KEY from the restored $name"
found=$(etcdctl --endpoints "$client" --command-timeout 5s get "$KEY" --keys-only) ||
	fail key "etcd on the restored $name did not answer a read of $KEY"
printf '%s\n' "$found" | grep -Fqx "$KEY" || fail key "the restored $name holds no $KEY"

echo "ok: $name, taken $(((now - taken) / 60)) minutes ago, at revision $revision with $keys keys, restores and serves $KEY"
