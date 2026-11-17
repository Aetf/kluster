# The backup bucket, read and written through B2's native API with `curl`.
# Sourced by the scripts that run in the bucket's container, in the curl image
# (`components.monitoring.stores`), for what `vmbackup` and `vmrestore` do not
# do themselves: whether a prefix holds any file, and a small file read or
# written beside a backup.
#
# It authenticates with the writer key the store's backup runs under, from the
# fields restic reads it from, `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`.
# The key is restricted to one bucket, so the authorization answers with that
# bucket's id and name.
#
# **No credential is ever an argument.** A process's arguments are readable
# by every process on the node that sees its PID, while its environment and the
# files under `WORK` are readable by its own user alone. So the key and the
# tokens B2 hands back are written, by the shell's own `printf`, into header
# files under `WORK`, which `curl` reads with `-H @<file>`; every other tool
# takes them on its standard input. The files are the user's own alone.
#
# Every call gives up after `CALL_TIMEOUT` seconds, and every function fails,
# naming what it could not read, where B2 does not answer.

umask 077
B2_API=${B2_API:-https://api.backblazeb2.com}
CALL_TIMEOUT=${CALL_TIMEOUT:-60}

# json_field NAME: the string value under NAME in the JSON object on stdin,
# whose names each occur once in the answers read here.
json_field() {
	tr -d '\n' | sed -n "s/.*\"$1\": *\"\([^\"]*\)\".*/\1/p"
}

# b2_call HEADERS URL [CURL OPTION...]: the body B2 answers URL with, the
# headers read from the file HEADERS.
b2_call() {
	headers=$1
	url=$2
	shift 2
	curl -sS --fail --max-time "$CALL_TIMEOUT" -H "@$headers" "$@" "$url"
}

# b2_authorize: authorize the writer key, setting B2_API_URL, B2_DOWNLOAD_URL,
# B2_BUCKET_ID and B2_BUCKET_NAME, and writing the account's token into the
# header file every later call reads.
b2_authorize() {
	basic=$(printf '%s:%s' "$AWS_ACCESS_KEY_ID" "$AWS_SECRET_ACCESS_KEY" | base64 | tr -d '\n')
	printf 'Authorization: Basic %s\n' "$basic" >"$WORK/b2-key"
	if ! answer=$(b2_call "$WORK/b2-key" "$B2_API/b2api/v2/b2_authorize_account"); then
		echo "the backup bucket refused the writer key, or did not answer" >&2
		return 1
	fi
	token=$(printf '%s' "$answer" | json_field authorizationToken)
	B2_API_URL=$(printf '%s' "$answer" | json_field apiUrl)
	B2_DOWNLOAD_URL=$(printf '%s' "$answer" | json_field downloadUrl)
	B2_BUCKET_ID=$(printf '%s' "$answer" | json_field bucketId)
	B2_BUCKET_NAME=$(printf '%s' "$answer" | json_field bucketName)
	if [ -z "$token" ] || [ -z "$B2_API_URL" ] || [ -z "$B2_DOWNLOAD_URL" ] || [ -z "$B2_BUCKET_ID" ] ||
		[ -z "$B2_BUCKET_NAME" ]; then
		echo "the backup bucket's authorization named no token, address or bucket" >&2
		return 1
	fi
	printf 'Authorization: %s\nContent-Type: application/json\n' "$token" >"$WORK/b2-account"
}

# b2_listed PREFIX: prints `yes` where the bucket holds a current file whose
# name starts with PREFIX, and `no` where it holds none.
b2_listed() {
	if ! answer=$(b2_call "$WORK/b2-account" "$B2_API_URL/b2api/v2/b2_list_file_names" \
		--data "{\"bucketId\":\"$B2_BUCKET_ID\",\"prefix\":\"$1\",\"maxFileCount\":1}"); then
		echo "the backup bucket did not list $1" >&2
		return 1
	fi
	case $answer in
	*'"fileName"'*) echo yes ;;
	*) echo no ;;
	esac
}

# b2_cat NAME: the contents of the current file NAME.
b2_cat() {
	if ! b2_call "$WORK/b2-account" "$B2_DOWNLOAD_URL/file/$B2_BUCKET_NAME/$1"; then
		echo "the backup bucket did not return $1" >&2
		return 1
	fi
}

# b2_put NAME FILE: the contents of FILE as the current file NAME.
b2_put() {
	if ! answer=$(b2_call "$WORK/b2-account" "$B2_API_URL/b2api/v2/b2_get_upload_url" \
		--data "{\"bucketId\":\"$B2_BUCKET_ID\"}"); then
		echo "the backup bucket gave no address to upload $1 to" >&2
		return 1
	fi
	upload_url=$(printf '%s' "$answer" | json_field uploadUrl)
	upload_token=$(printf '%s' "$answer" | json_field authorizationToken)
	sha=$(sha1sum <"$2" | cut -d ' ' -f 1)
	printf 'Authorization: %s\nX-Bz-File-Name: %s\nContent-Type: text/plain\nX-Bz-Content-Sha1: %s\n' \
		"$upload_token" "$1" "$sha" >"$WORK/b2-upload"
	if ! b2_call "$WORK/b2-upload" "$upload_url" --data-binary "@$2" >/dev/null; then
		echo "the backup bucket did not take $1" >&2
		return 1
	fi
}
