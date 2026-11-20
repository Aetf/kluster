#!/bin/sh
# The authentication canary's run, which `kluster.components.apps.authelia`
# mounts into its CronJob. Shell because the curl image it runs in carries no
# other interpreter.
#
# It requests the canary's protected path with no credential, at the Gateway's
# address under the portal's name, and passes on a refusal alone: a 401, or a
# 302 to the portal. Anything else -- the target's own answer, an error from
# the gateway, no answer at all -- fails the run, which is what the
# scheduled-job family's alerts read. Every name and address is a variable the
# CronJob sets: CANARY_HOST, CANARY_ADDRESS, CANARY_PATH and CANARY_PORTAL.
set -u

url="https://${CANARY_HOST}${CANARY_PATH}"
echo "auth-canary: requesting ${url} at ${CANARY_ADDRESS} with no credential"
if ! answer=$(curl --silent --show-error --max-time 20 --output /dev/null \
    --resolve "${CANARY_HOST}:443:${CANARY_ADDRESS}" \
    --write-out '%{http_code} %{redirect_url}' "${url}"); then
    echo "auth-canary: FAILED — no answer from ${CANARY_ADDRESS}"
    exit 1
fi
code=${answer%% *}
location=${answer#* }

case "${code}" in
401)
    echo "auth-canary: ok — refused with 401"
    exit 0
    ;;
302)
    case "${location}" in
    "${CANARY_PORTAL}"/*)
        echo "auth-canary: ok — refused with a redirect to the portal"
        exit 0
        ;;
    esac
    ;;
esac
echo "auth-canary: FAILED — answered ${code} ${location}"
exit 1
