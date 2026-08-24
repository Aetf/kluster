#!/usr/bin/env bash
# Mint (or rotate) the B2 *management* key — the credential the `physical`
# stack uses to declare buckets, lifecycle rules, and the prefix-scoped writer
# keys (docs/credentials.md §3).
#
# Scope: bucket and key administration, deliberately *no* file capabilities —
# the key that manages the backup buckets cannot read a byte out of them.
#
# The master application key is the offline-tier credential (credentials.md
# §2) and is never stored by this script: it is read from the environment into
# a throwaway account-info file that is deleted on exit.
#
# Usage:
#   B2_MASTER_KEY_ID=... B2_MASTER_KEY=... ./b2.sh mint
#   B2_MASTER_KEY_ID=... B2_MASTER_KEY=... ./b2.sh prune <key-id>  # after slots updated
#
# Rotation is a re-run: `mint` creates a fresh key, `prune` retires every
# older key of the same name once the new one is in its slots.

set -euo pipefail

KEY_NAME=${KEY_NAME:-kluster-management}

# Bucket + key administration. No listFiles/readFiles/writeFiles/deleteFiles:
# managing a bucket never requires touching its contents.
CAPABILITIES="listBuckets,readBuckets,writeBuckets,deleteBuckets"
CAPABILITIES+=",readBucketEncryption,writeBucketEncryption"
CAPABILITIES+=",readBucketRetentions,writeBucketRetentions"
CAPABILITIES+=",readBucketReplications,writeBucketReplications"
CAPABILITIES+=",readBucketNotifications,writeBucketNotifications"
CAPABILITIES+=",listKeys,writeKeys,deleteKeys"

B2_IMAGE=${B2_IMAGE:-docker.io/backblazeit/b2:latest}

workdir=$(mktemp -d)
trap 'rm -rf "$workdir"' EXIT

# The CLI runs containerized (no host install); --user 0:0 maps to the invoking
# user under rootless podman, which is what makes the mounted state readable.
b2() {
    podman run --rm --user 0:0 \
        -e HOME=/root \
        -e B2_ACCOUNT_INFO=/work/account_info \
        -e B2_APPLICATION_KEY_ID \
        -e B2_APPLICATION_KEY \
        -v "$workdir:/work" \
        "$B2_IMAGE" "$@"
}

require_master() {
    : "${B2_MASTER_KEY_ID:?set B2_MASTER_KEY_ID (from the offline kit)}"
    : "${B2_MASTER_KEY:?set B2_MASTER_KEY (from the offline kit)}"
    B2_APPLICATION_KEY_ID=$B2_MASTER_KEY_ID
    B2_APPLICATION_KEY=$B2_MASTER_KEY
    export B2_APPLICATION_KEY_ID B2_APPLICATION_KEY
    b2 account authorize >/dev/null
}

cmd_mint() {
    require_master
    echo "Minting '$KEY_NAME' with:" >&2
    echo "  $CAPABILITIES" >&2

    # `key create` prints "<keyID> <applicationKey>" — the only time the secret
    # is ever available.
    local out key_id key_secret
    out=$(b2 key create "$KEY_NAME" "$CAPABILITIES")
    key_id=$(awk '{print $1}' <<<"$out")
    key_secret=$(awk '{print $2}' <<<"$out")

    # Verify by using it: authorize as the new key and list buckets.
    B2_APPLICATION_KEY_ID=$key_id B2_APPLICATION_KEY=$key_secret \
        b2 account authorize >/dev/null
    b2 bucket list >/dev/null
    echo "verified: the new key authorizes and can list buckets" >&2

    cat <<EOF

key name : $KEY_NAME
key id   : $key_id
key      : $key_secret

Store in the slots (credentials.md §4), then '$0 prune $key_id':
  - Pulumi config secret: b2:applicationKeyId / b2:applicationKey (physical)
  - CI Environment secret: B2_APPLICATION_KEY_ID / B2_APPLICATION_KEY
EOF
}

cmd_prune() {
    local keep=${1:?usage: $0 prune <key-id-currently-in-the-slots>}
    require_master

    # Columns of `key list`: ID, name, ... (no value contains whitespace).
    b2 key list | awk -v name="$KEY_NAME" -v keep="$keep" \
        '$2 == name && $1 != keep {print $1}' | while read -r old; do
        echo "deleting superseded key $old" >&2
        b2 key delete "$old"
    done
}

case "${1:-}" in
mint) cmd_mint ;;
prune)
    shift
    cmd_prune "$@"
    ;;
*)
    echo "usage: $0 {mint|prune <key-id-to-keep>}" >&2
    exit 2
    ;;
esac
