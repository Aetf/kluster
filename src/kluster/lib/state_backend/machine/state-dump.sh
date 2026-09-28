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
# **Every object under the dump prefix holds at least one stack checkpoint,
# and this is the uploader that refuses anything else** (state-backend.md
# §5). The operator's `state.verify_dump` holds its archives to the same
# rule. A backend serving no stack -- a box replaced and not yet restored,
# or a site before its first `pulumi stack init` -- dumps to a plausible
# archive whose restore brings nothing back, and uploaded, it would become
# the newest object: the one every recovery takes. So the run reads the
# archive it is about to encrypt, counts the stack checkpoints in its rows,
# and fails without uploading when there are none; the newest object stays
# the last dump that held state. Reading an archive rather than a stream is
# why the dump lands in the clear beside its own ciphertext for the steps
# that read it instead of being piped straight into `age`.
#
# The upload credential holds `writeFiles` alone -- it cannot list, read, or
# delete, which is why the bucket is addressed by id (listing is not permitted)
# and why pruning is a bucket lifecycle rule rather than this script's job.
#
# `state-dump count-stacks` reads what `pg_restore --data-only` prints on
# standard input and prints how many stack checkpoints its rows hold: the
# parser below, reachable on its own so the suite can hold it to the
# operator's copy of the grammar. It reads `$PG_DATABASE` as the run does.
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

# Where the state is: the table Pulumi's Postgres backend keeps it in -- the
# backend's default name, since the connection string names no `table=`
# (`config.ClientBundle.url`) -- and the directory, under the database's
# own prefix, that holds one checkpoint key per stack. `state.STATE_TABLE`
# and `state.STACKS` are the same two, and the suite holds them equal.
STATE_TABLE='pulumi_state'
STACKS='.pulumi/stacks/'

log() { printf 'state-dump: %s\n' "$*"; }
die() { printf 'state-dump: %s\n' "$*" >&2; exit 1; }

# How many stack checkpoints the `pg_restore --data-only` output on standard
# input holds.
#
# The state table's rows are one `COPY <schema>.<table> (key, data,
# updated_at) FROM stdin;` block, a row per line with the key first and a
# tab after it, ended by a line holding `\.`; an archive with no such table
# prints no block. Every key is `<database>/<path>`, and a stack's
# checkpoint is `<database>/.pulumi/stacks/<project>/<name>.json`, or
# `.json.gz` or `.json.zst` when the backend compresses
# (`PULUMI_DIY_BACKEND_GZIP`, `PULUMI_DIY_BACKEND_ZSTD`). That is the reading
# `pulumi stack ls` makes: the meta, `.bak`, history and backup rows beside a
# checkpoint are not stacks, and a backend whose stacks were all removed
# keeps only `.bak` rows.
#
# **This is the operator's `state.checkpoints` with the keys counted instead
# of returned.** The grammar is copied line for line because nothing of this
# repository is installed on the appliance, and the alternative to a copy is
# no check here at all; `tests/test_state_dump.py` runs both over one table
# of inputs and compares the numbers, and `tests/test_state_roles.py` holds
# both to what the pinned `pulumi` writes.
count_stacks() {
  awk -v table="$STATE_TABLE" -v prefix="$PG_DATABASE/$STACKS" '
    inside && $0 == "\\." { inside = 0; next }
    inside {
      key = substr($0, 1, index($0 "\t", "\t") - 1)
      if (index(key, prefix) == 1 && substr(key, length(prefix) + 1) ~ /^.+\.json(\.gz|\.zst)?$/) count++
      next
    }
    $1 == "COPY" && $NF == "stdin;" { name = $2; sub(/^.*\./, "", name); inside = (name == table) }
    END { print count + 0 }
  '
}

# pg_dump to an archive, read it, then encrypt the archive to `$2` in `$1`.
#
# Separate steps rather than one pipeline: a stream cannot be read twice, and
# an object nobody read is one nobody discovers until a restore needs it. The
# plaintext is removed as soon as the ciphertext exists, so the peak is one
# copy of the state plus its (already compressed) encryption.
dump() {
  local spool=$1 ciphertext=$2
  local archive="$spool/state.dump" complaint="$spool/complaint"
  local -a recipients=()
  local line status stacks

  # A bare `read` strips the whitespace around a line; blank lines are
  # skipped, or age would be handed a recipient it rejects.
  while read -r line || [[ -n $line ]]; do
    if [[ -n $line ]]; then recipients+=(-r "$line"); fi
  done < "$RECIPIENTS"

  log "dumping $PG_DATABASE from $CONTAINER to $archive"
  podman exec "$CONTAINER" pg_dump -Fc -U "$PG_ROLE" "$PG_DATABASE" > "$archive" || die "pg_dump failed ($?)"

  # `pg_restore` comes from the same container as `pg_dump`; reading the
  # archive on standard input is what saves mounting the spool into it.
  # The listing is the check that `pg_restore` can read the archive at all.
  # It is not a truncation check: a custom-format archive keeps its table of
  # contents at the head, so a file cut to a few kilobytes still lists.
  log 'listing the archive'
  status=0
  podman exec -i "$CONTAINER" pg_restore --list < "$archive" > /dev/null 2> "$complaint" || status=$?
  if (( status != 0 )); then
    # With the output captured, what pg_restore said is the only account
    # of why it refused the archive, and a status on its own sends the
    # reader to a box nobody logs into to reproduce it by hand.
    die "pg_restore --list failed ($status): $(< "$complaint")"
  fi

  # The rows go straight into the parser rather than to a file: they are the
  # state in the clear, and the parser keeps nothing of them but a count.
  # This read is also the truncation check the listing is not: the database
  # has one data block, the state table's, so an archive cut anywhere past
  # its table of contents fails here -- possibly after printing every row --
  # and `pipefail` carries that status past the counter to the test below,
  # which is what refuses it whatever the count.
  log "counting the stack checkpoints in the archive's $STATE_TABLE rows"
  status=0
  stacks=$(podman exec -i "$CONTAINER" pg_restore --data-only --table="$STATE_TABLE" --file=- \
    < "$archive" 2> "$complaint" | count_stacks) || status=$?
  if (( status != 0 )); then
    die "pg_restore --data-only failed ($status): $(< "$complaint")"
  fi
  if (( stacks == 0 )); then
    die "the archive holds no stack checkpoint (no key under $PG_DATABASE/$STACKS in $STATE_TABLE), so" \
      'nothing was uploaded: every object under the dump prefix holds one (state-backend.md §5).' \
      'Either this box was replaced and its state is not restored yet -- `state-backend restore`' \
      'puts it back -- or no `pulumi stack init` has run against it yet'
  fi
  log "the archive holds $stacks stack checkpoint(s)"

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
  count-stacks) count_stacks ;;
  *) die "unknown mode: $1" ;;
esac
