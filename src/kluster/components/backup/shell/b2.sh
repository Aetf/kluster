# The backup bucket, read and written through B2's native API with `curl`.
# Sourced by the scripts that run in curl's image, or in one built on it, for
# what their tools do not do themselves: the metrics stores' scripts
# (`components.monitoring.stores`), for whether a prefix holds any file and a
# small file read or written beside a backup, which `vmbackup` and `vmrestore`
# do not do; and the etcd restore-verify (`components.backup.etcd`), for the
# newest snapshot's name and its download.
#
# It authenticates with the key its consumer runs under, from the fields
# restic reads a key from, `AWS_ACCESS_KEY_ID` and `AWS_SECRET_ACCESS_KEY`: a
# store's writer key, or the etcd snapshots' reader key. Each key is
# restricted to one bucket, so the authorization answers with that bucket's
# id, and with its name where B2 states one.
#
# **Every call is version 4 of the native API.** Each key is restricted
# through `bucketIds`, which makes it what B2 calls a multi-bucket application
# key, and B2 authorizes one of those on `b2_authorize_account` v4 alone,
# answering an earlier version with an error. v4's answer keeps the key's
# restriction under `apiInfo.storageApi.allowed`, whose `buckets` lists each
# bucket as an `id` and a `name`, beside that object's `apiUrl` and
# `downloadUrl`; the token is `authorizationToken`, at the top
# (https://www.backblaze.com/apidocs/b2-authorize-account). A key restricted
# with a single `bucketId` authorizes on v4 as well. The calls after it are on
# v4 too: a new version serves every call, and those it did not change behave
# under it as under the version before ("Native API Versions",
# https://www.backblaze.com/docs/cloud-storage-native-api-versions), and v4 is
# the version their reference pages describe. Those pages describe
# `b2_list_file_names`, `b2_get_upload_url` and `b2_download_file_by_id` as
# GETs that still take a POST whose JSON body carries the parameters, which is
# what is sent here; a download by id goes to `downloadUrl`, the others to
# `apiUrl`. `b2_upload_file` goes to the address `b2_get_upload_url` answers,
# and a download by name to `downloadUrl` under no version at all.
#
# **The bucket's name is optional, its id is not.** The answer lists each
# bucket's `name` beside its `id`, and B2's own example answers `null` for a
# name without saying when it does. Every call here but the download by name
# addresses the bucket by its id, so a key is refused only where its bucket has
# no id, and `b2_cat`, the download by name, refuses where no name was given.
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
B2_VERSION=v4

# json_field NAME: the string value under NAME in the JSON object on stdin,
# whose names each occur once in the answers read here.
json_field() {
	tr -d '\n' | sed -n "s/.*\"$1\": *\"\([^\"]*\)\".*/\1/p"
}

# json_strings NAME: every string value under NAME in the JSON object on
# stdin, one a line, in the order they occur. For a name that occurs once in
# each entry of an array and nowhere else, whose values hold no quote.
json_strings() {
	tr -d '\n' | grep -o "\"$1\": *\"[^\"]*\"" | sed "s/^\"$1\": *\"\(.*\)\"\$/\1/"
}

# json_buckets: the entries of the `buckets` array in the JSON object on stdin,
# one a line, each the text inside its braces, and nothing where the array is
# empty, `null` or absent. An entry holds a bucket's `id` and `name` alone, each
# a string or `null`, and neither holds a bracket, a brace or a quote: a name
# is letters, digits, hyphens and periods (`b2_create_bucket`), and an id is
# letters and digits. So the array ends at the first `]` after it opens, and
# each entry at its first `}`.
json_buckets() {
	tr '\n\r\t' '   ' | sed -n 's/.*"buckets" *: *\[\([^]]*\)\].*/\1/p' | tr '}' '\n' | sed -n 's/^[ ,]*{//p'
}

# b2_call HEADERS URL [CURL OPTION...]: the body B2 answers URL with, the
# headers read from the file HEADERS.
b2_call() {
	headers=$1
	url=$2
	shift 2
	curl -sS --fail --max-time "$CALL_TIMEOUT" -H "@$headers" "$@" "$url"
}

# b2_authorize: authorize the key, setting B2_API_URL, B2_DOWNLOAD_URL,
# B2_BUCKET_ID and B2_BUCKET_NAME, the last empty where the answer names no
# bucket, and writing the account's token into the header file every later
# call reads.
b2_authorize() {
	basic=$(printf '%s:%s' "$AWS_ACCESS_KEY_ID" "$AWS_SECRET_ACCESS_KEY" | base64 | tr -d '\n')
	printf 'Authorization: Basic %s\n' "$basic" >"$WORK/b2-key"
	if ! answer=$(b2_call "$WORK/b2-key" "$B2_API/b2api/$B2_VERSION/b2_authorize_account"); then
		echo "the backup bucket refused the key, or did not answer" >&2
		return 1
	fi
	token=$(printf '%s' "$answer" | json_field authorizationToken)
	B2_API_URL=$(printf '%s' "$answer" | json_field apiUrl)
	B2_DOWNLOAD_URL=$(printf '%s' "$answer" | json_field downloadUrl)
	if [ -z "$token" ] || [ -z "$B2_API_URL" ] || [ -z "$B2_DOWNLOAD_URL" ]; then
		echo "the backup bucket's authorization named no token or address" >&2
		return 1
	fi
	# One entry a line, and command substitution strips the last line's
	# newline, so a single entry holds none.
	buckets=$(printf '%s' "$answer" | json_buckets)
	case $buckets in
	'')
		echo "the backup bucket's authorization restricts the key to no bucket, where it is restricted to one" >&2
		return 1
		;;
	*'
'*)
		echo "the backup bucket's authorization restricts the key to more than one bucket, where it is restricted to one" >&2
		return 1
		;;
	esac
	B2_BUCKET_ID=$(printf '%s' "$buckets" | json_field id)
	B2_BUCKET_NAME=$(printf '%s' "$buckets" | json_field name)
	if [ -z "$B2_BUCKET_ID" ]; then
		echo "the backup bucket's authorization named no id for the key's bucket" >&2
		return 1
	fi
	printf 'Authorization: %s\nContent-Type: application/json\n' "$token" >"$WORK/b2-account"
}

# b2_listed PREFIX: prints `yes` where the bucket holds a current file whose
# name starts with PREFIX, and `no` where it holds none.
b2_listed() {
	if ! answer=$(b2_call "$WORK/b2-account" "$B2_API_URL/b2api/$B2_VERSION/b2_list_file_names" \
		--data "{\"bucketId\":\"$B2_BUCKET_ID\",\"prefix\":\"$1\",\"maxFileCount\":1}"); then
		echo "the backup bucket did not list $1" >&2
		return 1
	fi
	case $answer in
	*'"fileName"'*) echo yes ;;
	*) echo no ;;
	esac
}

# b2_newest PREFIX START: the id and the name of the last current file whose
# name starts with PREFIX and sorts at START or after, separated by a space,
# and nothing where there is none. B2 lists names in order and answers a page
# at a time, naming where the next page starts, so the last name is the last
# page's last.
b2_newest() {
	newest=
	start=$2
	while :; do
		if ! answer=$(b2_call "$WORK/b2-account" "$B2_API_URL/b2api/$B2_VERSION/b2_list_file_names" \
			--data "{\"bucketId\":\"$B2_BUCKET_ID\",\"prefix\":\"$1\",\"startFileName\":\"$start\",\"maxFileCount\":1000}"); then
			echo "the backup bucket did not list $1 from $start" >&2
			return 1
		fi
		name=$(printf '%s' "$answer" | json_strings fileName | tail -n 1)
		if [ -n "$name" ]; then
			newest="$(printf '%s' "$answer" | json_strings fileId | tail -n 1) $name"
		fi
		start=$(printf '%s' "$answer" | json_field nextFileName)
		[ -n "$start" ] || break
	done
	[ -z "$newest" ] || printf '%s\n' "$newest"
}

# b2_fetch_id ID FILE: the contents of the file whose id is ID, written to FILE.
b2_fetch_id() {
	if ! b2_call "$WORK/b2-account" "$B2_DOWNLOAD_URL/b2api/$B2_VERSION/b2_download_file_by_id" \
		--data "{\"fileId\":\"$1\"}" -o "$2"; then
		echo "the backup bucket did not return the file $1" >&2
		return 1
	fi
}

# b2_cat NAME: the contents of the current file NAME.
b2_cat() {
	if [ -z "$B2_BUCKET_NAME" ]; then
		echo "the backup bucket's authorization named no bucket, and $1 is read by its bucket's name" >&2
		return 1
	fi
	if ! b2_call "$WORK/b2-account" "$B2_DOWNLOAD_URL/file/$B2_BUCKET_NAME/$1"; then
		echo "the backup bucket did not return $1" >&2
		return 1
	fi
}

# b2_put NAME FILE: the contents of FILE as the current file NAME.
b2_put() {
	if ! answer=$(b2_call "$WORK/b2-account" "$B2_API_URL/b2api/$B2_VERSION/b2_get_upload_url" \
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
