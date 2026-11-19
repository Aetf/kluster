#!/bin/sh
# Merge the configuration the component renders into the one the instance
# keeps, as the init container of syncthing's pod
# (`kluster.components.apps.syncthing`). It runs in the `syncthing-config`
# image, yq's, which carries a busybox shell and no Python.
#
# $1 is the instance's configuration file and $2 the rendered one;
# RETIRED_DEVICES holds the device IDs the instance drops, separated by
# spaces.
#
# Each top-level element the rendered file holds replaces the instance's
# whole, and every other element -- the devices, the folders -- stays as the
# instance keeps it. A retired device leaves the device list and every
# folder's list. An instance with no configuration yet takes the rendered one
# as its initial state.
#
# A configuration file that exists and holds no device is refused, and left
# as it is: every configuration syncthing writes holds the instance's own
# device, and yq reads a file it cannot read whole -- emptied, or cut short --
# as nothing, so a merge over it would keep no device and no folder. The
# result is written beside the file and renamed over it, so a merge cut short
# leaves the file as it was.
set -eu
umask 077

config=$1
rendered=$2

if [ ! -f "$config" ]; then
    echo "$config does not exist yet: installing the rendered configuration as its initial state"
    cp "$rendered" "$config.merged"
    mv "$config.merged" "$config"
    exit 0
fi

if ! yq --input-format xml -e '.configuration.device' "$config" >/dev/null; then
    echo "yq reads no device in $config, which is no configuration syncthing wrote whole; refusing to merge over it" >&2
    exit 1
fi

echo "merging the rendered configuration into $config"
RENDERED=$rendered yq --input-format xml --output-format xml --xml-attribute-prefix '+@' '
  (.configuration) += load_xml(strenv(RENDERED)).configuration
  | (strenv(RETIRED_DEVICES) | split(" ")) as $retired
  | del(
      ..
      | select(
          tag == "!!map"
          and (key == "device" or (parent | key) == "device")
          and (.["+@id"] as $id | $retired | any_c(. == $id))
        )
    )
' "$config" >"$config.merged"
mv "$config.merged" "$config"
