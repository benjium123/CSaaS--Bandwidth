#!/usr/bin/env bash
# P40: let SignalWire's SIP signaling reach livekit-sip on UDP 5060.
#
# SignalWire publishes no fixed IP list; its docs say to resolve these names periodically.
# Runs ON THE BOX as root. Additive only: an address DNS stops returning is kept (DNS
# rotates, so a missing answer is not proof the address is gone). Idempotent.
#
#   bash deploy/signalwire_sip_firewall.sh                  # add any new addresses now
#   bash deploy/signalwire_sip_firewall.sh --install-cron   # and re-check every 15 minutes
set -euo pipefail

HOSTS="sip.signalwire.com relay.signalwire.com firewall.signalwire.com"
COMMENT="csaas sip signaling (signalwire)"

if [[ "${1:-}" == "--install-cron" ]]; then
  install -m 0755 "$0" /usr/local/sbin/csaas-signalwire-firewall
  echo "*/15 * * * * root /usr/local/sbin/csaas-signalwire-firewall >/dev/null 2>&1" \
    > /etc/cron.d/csaas-signalwire-firewall
  echo "cron installed: /etc/cron.d/csaas-signalwire-firewall"
fi

existing="$(ufw status | awk -v c="$COMMENT" 'index($0, c) && $1 == "5060/udp" {print $3}')"
added=0
for host in $HOSTS; do
  for ip in $(getent ahostsv4 "$host" | awk '{print $1}' | sort -u); do
    if ! grep -qx "$ip" <<<"$existing"; then
      ufw allow proto udp from "$ip" to any port 5060 comment "$COMMENT" >/dev/null
      existing="$existing"$'\n'"$ip"
      echo "allowed $ip ($host)"
      added=$((added + 1))
    fi
  done
done
echo "done: $added new, $(grep -c . <<<"$existing") SignalWire addresses allowed on 5060/udp"
