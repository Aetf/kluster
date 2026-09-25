#!/usr/bin/bash
# Dump the Pulumi state, verify it, encrypt it to the age recipients, upload to B2.
#
# Runs on the appliance from a systemd timer (physical/state-backend.md §5).
# Shell rather than Python because the box has no interpreter: Fedora CoreOS
# ships none, and nothing is installed here beyond the age binary
# `age-install.service` fetches. Everything else this uses -- bash, coreutils,
# curl, jq, gawk, sed, podman -- is in the image (state-backend.md §1
# names the rule, and the test that holds the template to it).
#
# **The nightly object is verified the same way the operator's is.** A dump is
# listed with `pg_restore --list` and a listing naming no table fails the run,
# which is what catches a dump of a database that has lost its tables -- the
# shape a box produces after a replacement nobody followed with a restore. The
# next reader of an object nobody listed is the restore that needed it, a
# retention window later. That check reads an archive rather than a stream, so
# the dump lands in the clear beside its own ciphertext for the two steps that
# read it instead of being piped straight into `age`.
#
# The upload credential holds `writeFiles` alone -- it cannot list, read, or
# delete, which is why the bucket is addressed by id (listing is not permitted)
# and why pruning is a bucket lifecycle rule rather than this script's job.
#
# `state-dump count-tables` reads a `pg_restore --list` listing on standard
# input and prints how many tables it names: the parser below, reachable on
# its own so the suite can hold it to the operator's copy of the grammar.
#
# Every stage announces itself before it starts. The journal is this box's
# only trace, and a run that hangs says where it hangs only if the stage
# said so first.

set -euo pipefail

AUTHORIZE_URL='https://api.backblazeb2.com/b2api/v3/b2_authorize_account'
CONTAINER='pgstate'

# Where the archive and its ciphertext sit while the run needs them. Not
# /tmp: that is a tmpfs sized from a 1 GB box's memory, and this holds the
# whole state twice over for the length of one dump. The unit that runs this
# sets no `PrivateTmp=`, which would otherwise settle the same question from
# the other side: `disconnected` backs the service's /var/tmp with a tmpfs
# too, and plain `yes` hands it a private /var/tmp instead of the host's
# (butane.yaml.j2).
#
# Seams for the suite, which runs this file on a workstation where the last
# two paths do not exist and where the spool is a directory of the case's own.
# The unit sets none of them: it spools to the host's /var/tmp, and it reads
# the template's recipients file and the age that `age-install.service` pins.
SPOOL=${STATE_DUMP_SPOOL:-/var/tmp}
RECIPIENTS=${STATE_DUMP_RECIPIENTS:-/etc/kluster/age-recipients.txt}
AGE=${STATE_DUMP_AGE:-/opt/bin/age}

# What an entry line calls a table in `pg_restore --list` output, and the
# word that follows it when the entry is the rows rather than the definition.
TABLE='TABLE'
DATA='DATA'

log() { printf 'state-dump: %s\n' "$*"; }
die() { printf 'state-dump: %s\n' "$*" >&2; exit 1; }

# How many tables the `pg_restore --list` output on standard input names.
#
# An entry line is `<id>; <catalogue oid> <oid> <what> <schema> <name>
# <owner>`, and `<what>` is one word for a table's definition and two --
# `TABLE DATA` -- for its rows. Comment lines, which is the whole header,
# start with the semicolon.
#
# **This is the operator's `state.tables` with the names counted instead of
# returned**, down to the same set: a table contributes its definition and
# its rows as two entries, so counting entries would answer twice what the
# other side answers once. The grammar is copied line for line because
# nothing of this repository is installed on the appliance, and the
# alternative to a copy is no check here at all; `tests/test_state_dump.py`
# runs both over one table of listings and compares the numbers, not their
# truthiness.
count_tables() {
  awk -v table="$TABLE" -v data="$DATA" '
    /^;/ { next }
    {
      at = index($0, ";")
      if (at == 0) next
      n = split(substr($0, at + 1), part)
      if (n < 4 || part[3] != table) next
      start = (part[4] == data) ? 5 : 4
      if (n >= start + 1) found[part[start] "." part[start + 1]] = 1
    }
    END { count = 0; for (name in found) count++; print count }
  '
}

# pg_dump to an archive, list it, then encrypt the archive to `$2` in `$1`.
#
# Three steps rather than one pipeline: a stream cannot be listed, and an
# unlistable object is one nobody discovers until a restore needs it. The
# plaintext is removed as soon as the ciphertext exists, so the peak is one
# copy of the state plus its (already compressed) encryption.
dump() {
  local spool=$1 ciphertext=$2
  local archive="$spool/state.dump" listing="$spool/listing" complaint="$spool/complaint"
  local -a recipients=()
  local line status

  # A bare `read` strips the whitespace around a line; blank lines are
  # skipped, or age would be handed a recipient it rejects.
  while read -r line || [[ -n $line ]]; do
    if [[ -n $line ]]; then recipients+=(-r "$line"); fi
  done < "$RECIPIENTS"

  log "dumping $PG_DATABASE from $CONTAINER to $archive"
  podman exec "$CONTAINER" pg_dump -Fc -U "$PG_ROLE" "$PG_DATABASE" > "$archive" || die "pg_dump failed ($?)"

  # `pg_restore` comes from the same container as `pg_dump`; reading the
  # archive on standard input is what saves mounting the spool into it.
  # What this catches is a listing that names no table -- a dump of a
  # database with nothing in it. It is not a truncation check: a
  # custom-format archive keeps its table of contents at the head, so a file
  # cut to a few kilobytes still lists.
  log 'listing the archive'
  status=0
  podman exec -i "$CONTAINER" pg_restore --list < "$archive" > "$listing" 2> "$complaint" || status=$?
  if (( status != 0 )); then
    # With the output captured, what pg_restore said is the only account
    # of why it refused the archive, and a status on its own sends the
    # reader to a box nobody logs into to reproduce it by hand.
    die "pg_restore --list failed ($status): $(< "$complaint")"
  fi
  if [[ $(count_tables < "$listing") == 0 ]]; then
    die 'the archive lists no tables, so it is not a dump of the state backend'
  fi

  log "encrypting to $(( ${#recipients[@]} / 2 )) recipient(s)"
  "$AGE" --encrypt "${recipients[@]}" < "$archive" > "$ciphertext" || die "age failed ($?)"
  rm -f "$archive"
}

# One B2 call. All three of them are JSON-in/JSON-out under a deadline, the
# upload included; curl's `--fail-with-body` keeps B2's error document, whose
# `code` is the diagnosis a bare status would lose.
b2() {
  local deadline=$1
  shift
  local answer
  if ! answer=$(curl -sS --fail-with-body --max-time "$deadline" "$@"); then
    die "B2 refused: $answer"
  fi
  printf '%s' "$answer"
}

upload() {
  local path=$1 name=$2
  local account api_url token target upload_url upload_token sha1 encoded

  log 'authorizing with B2'
  account=$(b2 120 -u "$B2_KEY_ID:$B2_KEY" "$AUTHORIZE_URL")
  api_url=$(jq -er '.apiInfo.storageApi.apiUrl' <<< "$account")
  token=$(jq -er '.authorizationToken' <<< "$account")

  # Addressed by id: the writeFiles-only key cannot resolve a bucket name.
  log 'asking B2 for an upload URL'
  target=$(b2 120 -H "Authorization: $token" -H 'Content-Type: application/json' \
    --data "$(jq -cn --arg id "$B2_BUCKET_ID" '{bucketId: $id}')" \
    "$api_url/b2api/v3/b2_get_upload_url")
  upload_url=$(jq -er '.uploadUrl' <<< "$target")
  upload_token=$(jq -er '.authorizationToken' <<< "$target")

  # The object name is percent-encoded with its slashes kept: they are the
  # object's path separator in B2, and `@uri` alone would encode them too.
  encoded=$(jq -rn --arg name "$name" '$name | @uri' | sed 's|%2F|/|g')
  sha1=$(sha1sum "$path" | cut -d ' ' -f 1)
  log "uploading $(stat -c %s "$path") bytes as $name"
  b2 600 -H "Authorization: $upload_token" -H "X-Bz-File-Name: $encoded" \
    -H 'Content-Type: application/octet-stream' -H "X-Bz-Content-Sha1: $sha1" \
    --data-binary "@$path" "$upload_url" > /dev/null
}

main() {
  local stamp name
  stamp=$(date -u +%Y%m%dT%H%M%SZ)
  name="$B2_PREFIX/$stamp.dump.age"
  # Not `local`: the trap runs after this function has returned, and after
  # an `exit` from any depth below it.
  spool=$(mktemp -d "$SPOOL/state-dump.XXXXXX")
  trap 'rm -rf "$spool"' EXIT
  dump "$spool" "$spool/state.dump.age"
  upload "$spool/state.dump.age" "$name"
  log "uploaded $name"
}

case ${1-} in
  '') main ;;
  count-tables) count_tables ;;
  *) die "unknown mode: $1" ;;
esac
