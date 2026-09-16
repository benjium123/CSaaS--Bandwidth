#!/usr/bin/env bash
# P39: point SignalWire phone numbers' webhooks at this CSaaS deployment.
#
# Runs ON THE BOX. Reads credentials from /opt/csaas/.env; never takes the token as an
# argument and never prints it. Idempotent: re-running sets the same values again.
#
#   bash deploy/signalwire_number_webhooks.sh <number-id> [<number-id> ...]
#
# A number id is the SignalWire phone-number resource id (the uuid shown under the number
# in the SignalWire dashboard's Phone Numbers list), NOT the E.164.
#
# What it sets, per number (SignalWire Compatibility API, IncomingPhoneNumbers update):
#   SmsUrl / SmsMethod            -> inbound texts + delivery receipts
#   VoiceUrl / VoiceMethod        -> inbound calls (today: the platform's "not configured
#                                    for inbound calls" announcement; the trunk phase
#                                    changes that, this URL does not)
#   StatusCallback / ...Method    -> call status callbacks
# All three URLs are bare (no query string): SignalWire signs the URL it calls, and the
# backend verifies against the CONFIGURED URL byte for byte (D81).
#
# After running, check each number's page in the SignalWire dashboard: "Handle messages
# using" / "Handle calls using" must read "LaML Webhooks". Whether this call flips that
# setting by itself is not documented, so look.
set -euo pipefail

ENV_FILE="${ENV_FILE:-/opt/csaas/.env}"

if [ "$#" -lt 1 ]; then
    echo "usage: $0 <signalwire-number-id> [...]" >&2
    exit 2
fi
if [ ! -r "$ENV_FILE" ]; then
    echo "cannot read $ENV_FILE" >&2
    exit 2
fi

# Parse only the keys we need; do not `source` the file (it is not guaranteed to be shell).
_get() { grep -E "^$1=" "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '"'"'"' \r' || true; }
PROJECT_ID="$(_get SIGNALWIRE_PROJECT_ID)"
API_TOKEN="$(_get SIGNALWIRE_API_TOKEN)"
SPACE="$(_get SIGNALWIRE_SPACE_URL)"
PUBLIC_BASE_URL="$(_get PUBLIC_BASE_URL)"
WEBHOOK_URL="$(_get SIGNALWIRE_WEBHOOK_URL)"

for name in PROJECT_ID API_TOKEN SPACE PUBLIC_BASE_URL; do
    if [ -z "${!name}" ]; then
        echo "missing $name in $ENV_FILE (SIGNALWIRE_* / PUBLIC_BASE_URL)" >&2
        exit 2
    fi
done
case "$SPACE" in
    http://*|https://*|*/) echo "SIGNALWIRE_SPACE_URL must be the bare host, e.g. sabine.signalwire.com" >&2; exit 2 ;;
esac

PUBLIC_BASE_URL="${PUBLIC_BASE_URL%/}"
SMS_URL="${WEBHOOK_URL:-$PUBLIC_BASE_URL/api/v1/webhooks/signalwire/messaging}"
VOICE_URL="$PUBLIC_BASE_URL/api/v1/webhooks/signalwire/voice"

# The signing URL the backend verifies texts against MUST equal what SignalWire calls.
if [ -n "$WEBHOOK_URL" ] && [ "$WEBHOOK_URL" != "$PUBLIC_BASE_URL/api/v1/webhooks/signalwire/messaging" ]; then
    echo "warning: SIGNALWIRE_WEBHOOK_URL differs from PUBLIC_BASE_URL-derived messaging URL;" >&2
    echo "         using SIGNALWIRE_WEBHOOK_URL ($WEBHOOK_URL) so the signature check matches." >&2
fi

echo "space:   $SPACE"
echo "sms url: $SMS_URL"
echo "voice:   $VOICE_URL"

# -u reads user:password from an env var via the netrc-free --user form; the token never
# appears in the process list because curl reads it from the config stdin below.
rc=0
for NUMBER_ID in "$@"; do
    echo "--- $NUMBER_ID"
    RESP="$(curl -sS -m 30 \
        --config <(printf 'user = "%s:%s"\n' "$PROJECT_ID" "$API_TOKEN") \
        -X POST "https://$SPACE/api/laml/2010-04-01/Accounts/$PROJECT_ID/IncomingPhoneNumbers/$NUMBER_ID.json" \
        --data-urlencode "SmsUrl=$SMS_URL" \
        --data-urlencode "SmsMethod=POST" \
        --data-urlencode "VoiceUrl=$VOICE_URL" \
        --data-urlencode "VoiceMethod=POST" \
        --data-urlencode "StatusCallback=$VOICE_URL" \
        --data-urlencode "StatusCallbackMethod=POST" \
        -w '\n%{http_code}')" || { echo "curl failed for $NUMBER_ID" >&2; rc=1; continue; }
    CODE="${RESP##*$'\n'}"
    BODY="${RESP%$'\n'*}"
    if [ "$CODE" != "200" ] && [ "$CODE" != "201" ]; then
        echo "HTTP $CODE for $NUMBER_ID" >&2
        echo "$BODY" | head -c 400 >&2; echo >&2
        rc=1
        continue
    fi
    # Read back what SignalWire stored - never trust the request, print the response.
    echo "$BODY" | python3 -c '
import json, sys
d = json.load(sys.stdin)
print("  number:   ", d.get("phone_number"))
print("  sms_url:  ", d.get("sms_url"))
print("  voice_url:", d.get("voice_url"))
print("  status_cb:", d.get("status_callback"))
' 2>/dev/null || echo "$BODY" | head -c 400
done
exit $rc
