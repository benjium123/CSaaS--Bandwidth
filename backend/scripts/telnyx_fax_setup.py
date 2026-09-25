"""Create (or find) the CSaaS Telnyx Fax Application. Idempotent, create-only.

The Telnyx account is SHARED with the CRM: this script only ever lists, creates or updates
the application named ``csaas-fax`` and never touches any other object.

Usage (inside the api container, where TELNYX_API_KEY and PUBLIC_BASE_URL are set):
    python scripts/telnyx_fax_setup.py                 # dry run: shows what it would do
    python scripts/telnyx_fax_setup.py --apply         # create/update, prints the id
Put the printed id into /opt/csaas/.env as TELNYX_FAX_CONNECTION_ID and restart api.
"""

from __future__ import annotations

import argparse
import os
import sys

import httpx

NAME = "csaas-fax"
BASE = "https://api.telnyx.com/v2"
WEBHOOK_PATH = "/api/v1/webhooks/telnyx/fax"


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--outbound-voice-profile-id", default=os.environ.get("TELNYX_FAX_OVP_ID", ""))
    args = ap.parse_args()
    key = os.environ.get("TELNYX_API_KEY", "").strip().strip('"')
    base_url = os.environ.get("PUBLIC_BASE_URL", "").rstrip("/")
    if not key or not base_url:
        print("TELNYX_API_KEY and PUBLIC_BASE_URL must be set", file=sys.stderr)
        return 2
    webhook = f"{base_url}{WEBHOOK_PATH}"
    h = {"Authorization": f"Bearer {key}"}
    with httpx.Client(timeout=30) as c:
        r = c.get(f"{BASE}/fax_applications", headers=h,
                  params={"filter[application_name][contains]": NAME, "page[size]": 250})
        r.raise_for_status()
        apps = [a for a in r.json().get("data", []) if a.get("application_name") == NAME]
        body: dict = {"application_name": NAME, "webhook_event_url": webhook, "active": True}
        if args.outbound_voice_profile_id:
            body["outbound"] = {"outbound_voice_profile_id": args.outbound_voice_profile_id}
        if apps:
            app = apps[0]
            print(f"found {NAME} id={app['id']} webhook={app.get('webhook_event_url')}")
            if app.get("webhook_event_url") != webhook or args.outbound_voice_profile_id:
                if args.apply:
                    u = c.patch(f"{BASE}/fax_applications/{app['id']}", headers=h, json=body)
                    u.raise_for_status()
                    print("updated")
                else:
                    print(f"would update -> {body}")
            print(f"TELNYX_FAX_CONNECTION_ID={app['id']}")
            return 0
        if not args.apply:
            print(f"would create {body}")
            return 0
        r = c.post(f"{BASE}/fax_applications", headers=h, json=body)
        r.raise_for_status()
        new_id = r.json()["data"]["id"]
        print(f"created {NAME}")
        print(f"TELNYX_FAX_CONNECTION_ID={new_id}")
        return 0


if __name__ == "__main__":
    raise SystemExit(main())
