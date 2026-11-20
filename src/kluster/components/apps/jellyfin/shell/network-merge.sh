#!/bin/sh
# Merge the network settings the component owns into the network
# configuration the server keeps, as the init container of jellyfin's pod
# (`kluster.components.apps.jellyfin`). It runs in the server's own image,
# whose tools are a Debian base's: a POSIX shell and awk, with no Python and
# no XML tool, so the file is read in the shape the server writes it.
#
# $1 is the server's network configuration and $2 the rendered one.
#
# Each element the rendered file holds replaces the element of that name in
# the server's file, where it stands, or is added before the end when the
# file has none, and every other element -- one the dashboard set, or one an
# older server wrote and this one ignores -- stays as the file holds it. A
# server with no network configuration yet takes the rendered one as its
# initial state.
#
# The shape is the one the server writes: an XML declaration, after a UTF-8
# byte order mark where the server's network-configuration migration wrote
# the file, a `NetworkConfiguration` element opening on a line of its own,
# each setting one element, either whole on its line or, for a list, an
# opening tag on its line, one item element per line and the closing tag on
# its line, and the root closing on the last line. A file of any other shape
# is refused and left as it is, an empty file or one cut short among them:
# the server reads a file it cannot parse as no configuration at all and
# takes every default, so a merge over it would keep nothing of the server's
# settings. The result is written beside the file, flushed to disk and renamed
# over it, so a merge cut short, or a node lost after the rename, leaves a
# whole file. It is the pod's user's, with mode 0600, since the file can hold
# a certificate's password.
set -eu
umask 077

config=$1
rendered=$2

if [ ! -e "$config" ]; then
    echo "$config does not exist yet: installing the rendered network configuration as its initial state"
    mkdir -p "${config%/*}"
    cp "$rendered" "$config.merged"
    mv "$config.merged" "$config"
    exit 0
fi

# Both files are read by `feed`, line by line: the rendered one first, in
# BEGIN, which collects each of its elements under its name; then the
# server's, which is printed as it is read, each element the rendered file
# holds printed from there instead. Anything outside the shape above is
# refused, by line, and a file whose root never closes is refused at the end.
program='
function refuse(file, why) {
    printf "%s: %s; refusing to merge over it\n", file, why > "/dev/stderr"
    refused = 1
    exit 1
}

function strip(s) {
    sub(/^[ \t]+/, "", s)
    sub(/[ \t]+$/, "", s)
    return s
}

# The kind of the trimmed line t, with NAME set to its element: "leaf" for an
# element whole on the line, "open" for an opening tag alone on it, and ""
# for anything else.
function kind(t,    rest, tail, body) {
    NAME = ""
    if (!match(t, /^<[A-Za-z_][A-Za-z0-9_.-]*/)) return ""
    NAME = substr(t, 2, RLENGTH - 1)
    rest = substr(t, RLENGTH + 1)
    if (rest ~ /^([ \t][^<>]*)?\/>$/) return "leaf"
    if (rest ~ /^([ \t][^<>]*)?>$/) return "open"
    tail = "</" NAME ">"
    if (length(rest) <= length(tail)) return ""
    if (substr(rest, length(rest) - length(tail) + 1) != tail) return ""
    body = substr(rest, 1, length(rest) - length(tail))
    if (body ~ /^([ \t][^<>]*)?>[^<]*$/) return "leaf"
    return ""
}

function emit(line) {
    if (merging) print line
}

function element(name, text) {
    if (!merging) {
        if (name in owned) refuse(rendered, "it holds " name " twice")
        owned[name] = text
        order[++count] = name
        return
    }
    if (name in owned) {
        if (!(name in done)) print owned[name]
        done[name] = 1
        return
    }
    print text
}

function feed(line, file,    t, k, i) {
    lines++
    t = strip(line)
    if (lines == 1) sub(/^\357\273\277/, "", t)
    if (state == "before") {
        if (t == "" || t ~ /^<\?xml[ \t].*\?>$/) { emit(line); return }
        if (kind(t) == "open" && NAME == "NetworkConfiguration") { state = "root"; emit(line); return }
        refuse(file, "line " lines " opens no NetworkConfiguration element")
    }
    if (state == "root") {
        if (t == "") { emit(line); return }
        if (t == "</NetworkConfiguration>") {
            state = "after"
            if (merging) for (i = 1; i <= count; i++) element(order[i], "")
            emit(line)
            return
        }
        k = kind(t)
        if (k == "leaf") { element(NAME, line); return }
        if (k == "open") { state = "list"; list = NAME; text = line; return }
        refuse(file, "line " lines " is no setting the server writes")
    }
    if (state == "list") {
        if (t == "</" list ">") { state = "root"; element(list, text "\n" line); return }
        if (kind(t) == "leaf") { text = text "\n" line; return }
        refuse(file, "line " lines " is no item of " list)
    }
    if (t == "") { emit(line); return }
    refuse(file, "line " lines " follows the end of its NetworkConfiguration element")
}

BEGIN {
    state = "before"
    while ((status = (getline line < rendered)) > 0) feed(line, rendered)
    if (status < 0) refuse(rendered, "it cannot be read")
    if (state != "after") refuse(rendered, "its NetworkConfiguration element never closes")
    merging = 1
    state = "before"
    lines = 0
}

{ feed($0, config) }

END {
    if (refused) exit 1
    if (state != "after") refuse(config, "its NetworkConfiguration element never closes")
}
'

echo "merging the rendered network settings into $config"
if ! awk -v rendered="$rendered" -v config="$config" "$program" "$config" >"$config.merged"; then
    rm -f "$config.merged"
    exit 1
fi
sync "$config.merged"
mv "$config.merged" "$config"
