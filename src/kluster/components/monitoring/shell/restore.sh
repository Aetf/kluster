#!/bin/sh
# The restore of a VictoriaMetrics store, the second of its pod's two init
# containers, in `vmrestore`'s image (`components.monitoring.stores`). The
# arguments are the `vmrestore` command, run where `restore-check.sh`, the
# first, decided `restore` in `WORK/decision`. A decision that is neither
# `restore` nor `start` fails, so the store does not start.
set -eu

decision=$(cat "$WORK/decision")
case $decision in
restore) exec "$@" ;;
start) echo "the store starts without a restore" ;;
*)
	echo "no decision on whether to restore the store" >&2
	exit 1
	;;
esac
