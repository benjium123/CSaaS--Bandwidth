#!/usr/bin/env python3
"""P44a carrier-side fraud limits (defense in depth behind destination_policy.py).

The app refuses international and premium destinations itself; this script makes the
CARRIER refuse them too, so a bug, a leaked SIP credential or a direct API call cannot
run up an IRSF bill either.

Telnyx objects touched (only ones whose name starts with "csaas" - the account is shared
with the REI CRM and its objects must never change):
  - outbound voice profile(s): whitelisted_destinations=["US"], max_destination_rate,
    daily_spend_limit (+ enabled), concurrent_call_limit
  - messaging profile(s): whitelisted_destinations=["US"]

SignalWire has no public API for geo permissions: set them in the dashboard
(Space -> Settings -> Voice/Messaging geographic permissions: United States only).

Usage (stdlib only):
  TELNYX_API_KEY=... python3 fraud_carrier_limits.py show  --ovp ID... --mp ID...
  TELNYX_API_KEY=... python3 fraud_carrier_limits.py apply --ovp ID... --mp ID... \
        [--daily-spend 100] [--max-rate 0.05] [--concurrent 20]
`show` only reads. `apply` prints the diff, then writes.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.telnyx.com/v2"
COUNTRIES = ["US"]


def _call(method: str, path: str, body: dict | None = None) -> dict:
    key = os.environ.get("TELNYX_API_KEY", "").strip()
    if not key:
        sys.exit("TELNYX_API_KEY is not set")
    req = urllib.request.Request(
        API + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read() or b"{}").get("data") or {}
    except urllib.error.HTTPError as exc:
        sys.exit(f"{method} {path} -> {exc.code}: {exc.read().decode()[:500]}")


def _guard(obj: dict, kind: str, oid: str) -> None:
    name = str(obj.get("name") or "")
    if not name.lower().startswith("csaas"):
        sys.exit(f"REFUSED: {kind} {oid} is named {name!r}, not a csaas object")


OVP_FIELDS = (
    "name",
    "whitelisted_destinations",
    "max_destination_rate",
    "daily_spend_limit",
    "daily_spend_limit_enabled",
    "concurrent_call_limit",
)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["show", "apply"])
    ap.add_argument("--ovp", nargs="*", default=[], help="outbound voice profile ids")
    ap.add_argument("--mp", nargs="*", default=[], help="messaging profile ids")
    ap.add_argument("--daily-spend", default="100", help="USD per day per profile")
    ap.add_argument("--max-rate", type=float, default=0.05, help="max USD/min per destination")
    ap.add_argument("--concurrent", type=int, default=20)
    args = ap.parse_args()

    for oid in args.ovp:
        cur = _call("GET", f"/outbound_voice_profiles/{oid}")
        _guard(cur, "outbound voice profile", oid)
        print(f"OVP {oid}:", json.dumps({k: cur.get(k) for k in OVP_FIELDS}))
        if args.mode == "apply":
            want = {
                "whitelisted_destinations": COUNTRIES,
                "max_destination_rate": args.max_rate,
                "daily_spend_limit": str(args.daily_spend),
                "daily_spend_limit_enabled": True,
                "concurrent_call_limit": args.concurrent,
            }
            print("  ->", json.dumps(want))
            new = _call("PATCH", f"/outbound_voice_profiles/{oid}", want)
            print("  now:", json.dumps({k: new.get(k) for k in OVP_FIELDS}))

    for oid in args.mp:
        cur = _call("GET", f"/messaging_profiles/{oid}")
        _guard(cur, "messaging profile", oid)
        print(f"MP {oid}:", json.dumps({"name": cur.get("name"),
                                        "whitelisted_destinations": cur.get("whitelisted_destinations")}))
        if args.mode == "apply":
            new = _call("PATCH", f"/messaging_profiles/{oid}", {"whitelisted_destinations": COUNTRIES})
            print("  now:", json.dumps({"whitelisted_destinations": new.get("whitelisted_destinations")}))


if __name__ == "__main__":
    main()
