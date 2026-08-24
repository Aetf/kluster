#!/usr/bin/env bash
# Write credentials into the cluster's dedicated KeePassXC database — the
# canonical offline store (docs/credentials.md §2.1), scripted so "update the
# offline store" is a slot like any other rather than a manual copy-paste.
#
# Sourced by the per-credential scripts; not run directly.
#
#   KLUSTER_KDBX=/path/to/kluster.kdbx
#   source kdbx.sh
#   kdbx_unlock                       # asks for the master password once
#   kdbx_put root/oci "OCI API key" "$user_ocid" "$private_key"
#
# Requires keepassxc-cli (the KeePassXC package) and therefore runs on the
# machine that holds the database — the same machine that holds the kit.
# Re-issuing the kit stays a copy of this file onto both USB sticks.

: "${KLUSTER_KDBX:?set KLUSTER_KDBX to the cluster KeePassXC database}"

_KDBX_PASSWORD=""

kdbx_unlock() {
    if [[ -n $_KDBX_PASSWORD ]]; then
        return
    fi
    if [[ ! -f $KLUSTER_KDBX ]]; then
        echo "no database at $KLUSTER_KDBX" >&2
        return 1
    fi
    read -rs -p "master password for $(basename "$KLUSTER_KDBX"): " _KDBX_PASSWORD
    echo >&2
    # Fail early on a wrong password rather than midway through a rotation.
    if ! printf '%s\n' "$_KDBX_PASSWORD" | keepassxc-cli ls -q "$KLUSTER_KDBX" >/dev/null; then
        _KDBX_PASSWORD=""
        echo "could not unlock $KLUSTER_KDBX" >&2
        return 1
    fi
}

# kdbx_put <group/title> <username> <secret>
#
# Idempotent: creates the entry, or replaces the password of an existing one,
# so a rotation playbook re-runs the same script.
kdbx_put() {
    local path=$1 username=$2 secret=$3
    kdbx_unlock

    local verb=add
    if printf '%s\n' "$_KDBX_PASSWORD" | keepassxc-cli show -q "$KLUSTER_KDBX" "$path" >/dev/null 2>&1; then
        verb=edit
    fi

    # keepassxc-cli reads the database password first, then the entry's.
    printf '%s\n%s\n' "$_KDBX_PASSWORD" "$secret" |
        keepassxc-cli "$verb" -q "$KLUSTER_KDBX" "$path" \
            --username "$username" --password-prompt

    echo "kdbx: ${verb}ed $path" >&2
}
