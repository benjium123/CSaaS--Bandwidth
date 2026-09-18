"""P40: wire SignalWire calling into LiveKit, idempotently. Dry run unless --apply.

Runs INSIDE the api container on the box (it needs the app's settings and LiveKit client):

    docker cp deploy/signalwire_voice_setup.py csaas-api-1:/tmp/sw_setup.py
    docker exec csaas-api-1 python /tmp/sw_setup.py +16824231003 +14692103654            # plan
    docker exec csaas-api-1 python /tmp/sw_setup.py --apply +16824231003 +14692103654    # do it

What it sets up (each step is skipped when it already exists):

  SignalWire
    1. SWML script `csaas-livekit-outbound`: bridges a call from our SIP credential to the
       PSTN. Caller id = the SIP From user livekit-sip sends (the CSaaS number the call is
       placed from); destination = the Request-URI user.
    2. SWML script `csaas-livekit-inbound`: sends a call on the number to livekit-sip.
    3. SIP credential `csaas-livekit` whose outbound calls run script 1. Password is random,
       never printed, and lives only in SignalWire and the LiveKit trunk.
    4. Each number's CALLING handler -> script 2. Texting handlers are not touched.
  LiveKit
    5. Outbound trunk `signalwire-out` -> <space>.sip.signalwire.com with that credential.
    6. Inbound trunk `signalwire-in` for the numbers.
    7. Dispatch rule `signalwire-in-individual` (room prefix `call-`) for that trunk only;
       the Telnyx rule is left alone.

Prints the outbound trunk id at the end: put it in /opt/csaas/.env as
LIVEKIT_SIP_SIGNALWIRE_TRUNK_ID and restart the api. Secrets are never printed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import secrets
import sys

import httpx

from app.config import Settings
from app.voice_plane.livekit_api import LiveKitApi

BOX_SIP_HOST = "144.126.152.175:5060"
OUTBOUND_SCRIPT = "csaas-livekit-outbound"
INBOUND_SCRIPT = "csaas-livekit-inbound"
SIP_USERNAME = "csaas-livekit"
LK_OUT, LK_IN, LK_RULE = "signalwire-out", "signalwire-in", "signalwire-in-individual"

OUTBOUND_SWML = {
    "version": "1.0.0",
    "sections": {
        "main": [
            {
                "connect": {
                    "answer_on_bridge": True,
                    "from": "${call.sip_data.sip_from_user}",
                    "to": "${call.sip_data.sip_req_user}",
                }
            }
        ]
    },
}
INBOUND_SWML = {
    "version": "1.0.0",
    "sections": {
        "main": [{"connect": {"to": f"sip:%{{call.to}}@{BOX_SIP_HOST};transport=udp"}}]
    },
}


class Plan:
    def __init__(self, apply: bool) -> None:
        self.apply = apply

    def step(self, text: str) -> bool:
        print(("DO   " if self.apply else "WOULD ") + text)
        return self.apply


def _items(body: object) -> list[dict]:
    if isinstance(body, dict):
        body = body.get("data", body.get("items", []))
    return [b for b in body if isinstance(b, dict)] if isinstance(body, list) else []


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("numbers", nargs="+", help="E.164 SignalWire numbers to wire")
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    plan = Plan(args.apply)

    s = Settings()
    space = s.signalwire_space_url.strip()
    if not (space and s.signalwire_project_id and s.signalwire_api_token.get_secret_value()):
        print("SIGNALWIRE_* credentials are not set", file=sys.stderr)
        return 2
    if not (s.livekit_url and s.livekit_api_secret.get_secret_value()):
        print("LIVEKIT_* is not set", file=sys.stderr)
        return 2
    sip_domain = space.replace(".signalwire.com", ".sip.signalwire.com")

    sw = httpx.AsyncClient(
        base_url=f"https://{space}",
        auth=(s.signalwire_project_id, s.signalwire_api_token.get_secret_value()),
        timeout=30,
    )
    lk = LiveKitApi(
        url=s.livekit_url,
        api_key=s.livekit_api_key,
        api_secret=s.livekit_api_secret.get_secret_value(),
    )

    async def sw_call(method: str, path: str, json: dict | None = None) -> dict | list:
        r = await sw.request(method, path, json=json)
        if r.status_code >= 400:
            raise RuntimeError(f"SignalWire {method} {path} -> {r.status_code}: {r.text[:400]}")
        return r.json() if r.content else {}

    try:
        # ---- 1 + 2: SWML scripts ---------------------------------------------------------
        scripts = {
            (x.get("display_name") or x.get("name")): x
            for x in _items(await sw_call("GET", "/api/fabric/resources/swml_scripts"))
        }
        script_ids: dict[str, str | None] = {}
        for name, contents in ((OUTBOUND_SCRIPT, OUTBOUND_SWML), (INBOUND_SCRIPT, INBOUND_SWML)):
            if name in scripts:
                script_ids[name] = scripts[name]["id"]
                if plan.step(f"update SWML script {name} to the current contents"):
                    await sw_call(
                        "PUT",
                        f"/api/fabric/resources/swml_scripts/{scripts[name]['id']}",
                        {"name": name, "contents": json.dumps(contents)},
                    )
            else:
                script_ids[name] = None
                if plan.step(f"create SWML script {name}"):
                    made = await sw_call(
                        "POST",
                        "/api/fabric/resources/swml_scripts",
                        {"name": name, "contents": json.dumps(contents), "script_type": "calling"},
                    )
                    script_ids[name] = made["id"]

        # ---- 5 (lookup first: the credential password is only known when we set it) -----
        async def lk_list(method: str) -> dict[str, dict]:
            return {x["name"]: x for x in _items(await lk._twirp("SIP", method, {}))}

        out_trunks = await lk_list("ListSIPOutboundTrunk")
        in_trunks = await lk_list("ListSIPInboundTrunk")
        rules = await lk_list("ListSIPDispatchRule")

        # ---- 3: SIP credential ----------------------------------------------------------
        endpoints = {
            x.get("username"): x
            for x in _items(await sw_call("GET", "/api/fabric/resources/sip_endpoints"))
        }
        legacy = {
            x.get("username"): x
            for x in _items(await sw_call("GET", "/api/relay/rest/endpoints/sip"))
        }
        password: str | None = None
        endpoint = endpoints.get(SIP_USERNAME) or legacy.get(SIP_USERNAME)
        need_password = LK_OUT not in out_trunks
        if endpoint is None and not need_password:
            print(
                f"ABORT: LiveKit trunk {LK_OUT} exists but SIP credential {SIP_USERNAME} does not;"
                " its password cannot match. Delete the trunk in LiveKit and re-run.",
                file=sys.stderr,
            )
            return 1
        if endpoint is None:
            password = secrets.token_urlsafe(24)
            if plan.step(f"create SIP credential {SIP_USERNAME} (outbound -> {OUTBOUND_SCRIPT})"):
                body = {
                    "username": SIP_USERNAME,
                    "password": password,
                    "caller_id": args.numbers[0],
                    "send_as": args.numbers[0],
                    "encryption": "optional",
                    "codecs": ["PCMU", "PCMA", "OPUS"],
                    "call_handler": "resource",
                    "calling_handler_resource_id": script_ids[OUTBOUND_SCRIPT],
                }
                try:
                    endpoint = await sw_call("POST", "/api/fabric/resources/sip_endpoints", body)
                except RuntimeError as exc:
                    print(f"     fabric create refused ({exc}); using the legacy endpoint API")
                    legacy_body = {
                        k: v for k, v in body.items()
                        if k not in ("call_handler", "calling_handler_resource_id")
                    }
                    endpoint = await sw_call("POST", "/api/relay/rest/endpoints/sip", legacy_body)
                    await sw_call(
                        "POST",
                        f"/api/fabric/resources/{script_ids[OUTBOUND_SCRIPT]}/sip_endpoints",
                        {"sip_endpoint_id": endpoint["id"]},
                    )
        elif need_password:
            password = secrets.token_urlsafe(24)
            if plan.step(f"reset the password of existing SIP credential {SIP_USERNAME}"):
                await sw_call(
                    "PUT", f"/api/relay/rest/endpoints/sip/{endpoint['id']}", {"password": password}
                )
        if endpoint is not None and plan.apply:
            fresh = _items(await sw_call("GET", "/api/fabric/resources/sip_endpoints"))
            mine = next((x for x in fresh if x.get("username") == SIP_USERNAME), None)
            handler = (mine or {}).get("call_handler")
            target = (mine or {}).get("calling_handler_resource_id")
            if handler != "resource" or target != script_ids[OUTBOUND_SCRIPT]:
                if plan.step(f"point {SIP_USERNAME}'s outbound calls at {OUTBOUND_SCRIPT}"):
                    await sw_call(
                        "POST",
                        f"/api/fabric/resources/{script_ids[OUTBOUND_SCRIPT]}/sip_endpoints",
                        {"sip_endpoint_id": (mine or endpoint)["id"]},
                    )

        # ---- 4: numbers' calling handler --------------------------------------------------
        addresses = _items(await sw_call("GET", "/api/fabric/phone_number_addresses?page_size=100"))
        for number in args.numbers:
            calling = next(
                (a for a in addresses
                 if a.get("phone_number") == number and a.get("handler_type") == "calling"),
                None,
            )
            if calling is None:
                if plan.step(f"link {number} calls -> {INBOUND_SCRIPT}"):
                    await sw_call(
                        "POST",
                        "/api/fabric/phone_number_addresses",
                        {"resource_id": script_ids[INBOUND_SCRIPT], "handler_type": "calling",
                         "number": number},
                    )
            elif calling.get("resource_id") != script_ids[INBOUND_SCRIPT]:
                if plan.step(f"switch {number} calls -> {INBOUND_SCRIPT} (texting unchanged)"):
                    await sw_call(
                        "PATCH",
                        f"/api/fabric/phone_number_addresses/{calling['id']}",
                        {"resource_id": script_ids[INBOUND_SCRIPT]},
                    )

        # ---- 5-7: LiveKit ------------------------------------------------------------------
        out_id = out_trunks.get(LK_OUT, {}).get("sip_trunk_id")
        if out_id is None:
            if plan.step(f"create LiveKit outbound trunk {LK_OUT} -> {sip_domain}"):
                made = await lk._twirp("SIP", "CreateSIPOutboundTrunk", {"trunk": {
                    "name": LK_OUT,
                    "address": sip_domain,
                    "transport": "SIP_TRANSPORT_UDP",
                    "numbers": args.numbers,
                    "auth_username": SIP_USERNAME,
                    "auth_password": password,
                }})
                out_id = made["sip_trunk_id"]
        elif sorted(out_trunks[LK_OUT].get("numbers") or []) != sorted(args.numbers):
            print(f"NOTE {LK_OUT} numbers differ from the ones given; update them in LiveKit")

        in_id = in_trunks.get(LK_IN, {}).get("sip_trunk_id")
        if in_id is None:
            if plan.step(f"create LiveKit inbound trunk {LK_IN}"):
                made = await lk._twirp(
                    "SIP",
                    "CreateSIPInboundTrunk",
                    {"trunk": {"name": LK_IN, "numbers": args.numbers}},
                )
                in_id = made["sip_trunk_id"]

        if LK_RULE not in rules:
            if plan.step(f"create LiveKit dispatch rule {LK_RULE} for {LK_IN}"):
                rule = {
                    "name": LK_RULE,
                    "rule": {"dispatch_rule_individual": {"room_prefix": "call-"}},
                    "trunk_ids": [in_id],
                }
                try:
                    await lk._twirp("SIP", "CreateSIPDispatchRule", {"dispatch_rule": rule})
                except Exception:  # noqa: BLE001 - older livekit-sip: flat request shape
                    await lk._twirp("SIP", "CreateSIPDispatchRule", rule)

        print()
        if out_id:
            print(f"LIVEKIT_SIP_SIGNALWIRE_TRUNK_ID={out_id}")
            if getattr(s, "livekit_sip_signalwire_trunk_id", "") != out_id:
                print("  -> set that in /opt/csaas/.env, then restart csaas-api-1")
        elif not plan.apply:
            print("dry run: nothing changed. Re-run with --apply.")
        return 0
    finally:
        await sw.aclose()
        await lk.aclose()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
