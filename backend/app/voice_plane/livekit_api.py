"""
Minimal LiveKit server API client.

No LiveKit SDK on purpose: the API is JWT + five Twirp POSTs; a dependency
would drag grpc/protobuf into the backend and make MockTransport testing
impossible-to-cheap.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from collections.abc import Mapping
from typing import Any

import httpx
import jwt
import structlog

logger = structlog.get_logger("voice_plane.livekit")


def mint_access_token(
    *,
    api_key: str,
    api_secret: str,
    identity: str,
    name: str = "",
    room: str = "",
    ttl_seconds: int = 3600,
    can_publish: bool = True,
    can_subscribe: bool = True,
    room_admin: bool = False,
    admin_grants: dict | None = None,
) -> str:
    now = int(time.time())
    claims: dict[str, Any] = {
        "iss": api_key,
        "sub": identity,
        "jti": identity,
        "nbf": now - 10,
        "exp": now + min(ttl_seconds, 3600),
        "name": name or identity,
    }

    if admin_grants is not None:
        claims["video"] = admin_grants
    elif room:
        claims["video"] = {
            "room": room,
            "roomJoin": True,
            "canPublish": can_publish,
            "canSubscribe": can_subscribe,
            "roomAdmin": room_admin,
        }
    else:
        claims["video"] = {
            "canPublish": can_publish,
            "canSubscribe": can_subscribe,
            "roomAdmin": room_admin,
        }

    return jwt.encode(claims, api_secret, algorithm="HS256")


def admin_token(api_key: str, api_secret: str) -> str:
    grants = {
        "roomCreate": True,
        "roomList": True,
        "roomAdmin": True,
        "room": "*",
        "roomJoin": False,
    }
    return mint_access_token(
        api_key=api_key,
        api_secret=api_secret,
        identity="csaas-backend",
        admin_grants=grants,
    )


class LiveKitApiError(RuntimeError):
    def __init__(self, status: int, message: str) -> None:
        self.status = status
        super().__init__(message)


class LiveKitApi:
    def __init__(
        self,
        *,
        url: str,
        api_key: str,
        api_secret: str,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.api_key = api_key
        self.api_secret = api_secret
        self.http_url = (
            url.replace("ws://", "http://", 1)
            .replace("wss://", "https://", 1)
            .rstrip("/")
        )
        self._client = client
        self._owns_client = client is None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient()
        return self._client

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None

    async def _twirp(self, service: str, method: str, body: dict) -> dict:
        url = f"{self.http_url}/twirp/livekit.{service}/{method}"
        headers = {
            "Authorization": f"Bearer {admin_token(self.api_key, self.api_secret)}",
            "Content-Type": "application/json",
        }
        client = await self._get_client()
        try:
            resp = await client.post(url, json=body, headers=headers)
        except httpx.HTTPError as exc:
            raise LiveKitApiError(0, str(exc)) from exc

        if resp.status_code < 200 or resp.status_code >= 300:
            raise LiveKitApiError(resp.status_code, resp.text[:255])

        if not resp.content:
            return {}
        return resp.json()

    async def create_room(
        self,
        name: str,
        *,
        empty_timeout: int = 300,
        metadata: str = "",
    ) -> dict:
        return await self._twirp(
            "RoomService",
            "CreateRoom",
            {"name": name, "empty_timeout": empty_timeout, "metadata": metadata},
        )

    async def delete_room(self, name: str) -> dict:
        return await self._twirp("RoomService", "DeleteRoom", {"room": name})

    async def list_rooms(self) -> list[dict]:
        data = await self._twirp("RoomService", "ListRooms", {})
        return data.get("rooms", [])

    async def remove_participant(self, room: str, identity: str) -> dict:
        return await self._twirp(
            "RoomService",
            "RemoveParticipant",
            {"room": room, "identity": identity},
        )

    async def update_subscriptions(
        self, *, room: str, identity: str, track_sids: list[str], subscribe: bool
    ) -> dict:
        """Force-(un)subscribe a participant from specific tracks (P12 DR-9 whisper:
        the caller's SIP participant is server-side denied the supervisor's track —
        never left to client politeness)."""
        return await self._twirp(
            "RoomService",
            "UpdateSubscriptions",
            {
                "room": room,
                "identity": identity,
                "track_sids": track_sids,
                "subscribe": subscribe,
            },
        )

    async def create_sip_participant(
        self,
        *,
        trunk_id: str,
        call_to: str,
        room: str,
        from_number: str,
        identity: str,
        participant_name: str = "",
        ringing_timeout_seconds: int = 45,
        wait_until_answered: bool = False,
    ) -> dict:
        return await self._twirp(
            "SIP",
            "CreateSIPParticipant",
            {
                "sip_trunk_id": trunk_id,
                "sip_call_to": call_to,
                "room_name": room,
                "sip_number": from_number,
                "participant_identity": identity,
                "participant_name": participant_name or identity,
                "ringing_timeout": f"{ringing_timeout_seconds}s",
                "wait_until_answered": wait_until_answered,
            },
        )

    async def create_agent_dispatch(
        self, *, room: str, agent_name: str, metadata: str = ""
    ) -> dict:
        """Explicitly dispatch a named agent worker (P7 echo agent, P8 AI agent) into a
        room. Workers register with agent_name and receive a job when dispatched."""
        return await self._twirp(
            "AgentDispatchService",
            "CreateDispatch",
            {"room": room, "agent_name": agent_name, "metadata": metadata},
        )

    async def transfer_sip_participant(
        self,
        *,
        room: str,
        identity: str,
        transfer_to: str,
    ) -> dict:
        return await self._twirp(
            "SIP",
            "TransferSIPParticipant",
            {
                "room_name": room,
                "participant_identity": identity,
                "transfer_to": transfer_to,
            },
        )


def verify_webhook(
    headers: Mapping[str, str],
    raw_body: bytes,
    *,
    api_key: str,
    api_secret: str,
) -> dict | None:
    auth_header = None
    for key, value in headers.items():
        if key.lower() == "authorization":
            auth_header = value
            break

    if not auth_header:
        return None

    token = auth_header
    if token.startswith("Bearer "):
        token = token[6:]
    token = token.strip()

    if not token:
        return None

    try:
        # LiveKit always sets iss (= API key) and exp (5-minute validity); requiring both
        # means a leaked long-lived token cannot be replayed indefinitely.
        claims = jwt.decode(
            token,
            api_secret,
            algorithms=["HS256"],
            options={"verify_aud": False, "verify_iss": False, "require": ["exp"]},
        )
    except jwt.PyJWTError as exc:
        logger.warning("livekit_webhook_jwt_decode_failed", error=str(exc))
        return None

    if claims.get("iss") != api_key:
        return None

    digest = hashlib.sha256(raw_body).digest()
    expected_sha256 = claims.get("sha256")
    if not isinstance(expected_sha256, str):
        return None

    # LiveKit encodes the digest with standard base64 only (source-verified); tolerant
    # multi-encoding matching was dead code and is gone.
    if not hmac.compare_digest(expected_sha256, base64.b64encode(digest).decode()):
        return None

    try:
        return json.loads(raw_body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return None


def room_name_for_call(call_id: object) -> str:
    return f"call-{str(call_id)}"
