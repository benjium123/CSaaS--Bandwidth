# Research — Telnyx (BACKUP carrier)

Researched 2026-08-26. Facts marked `⚠UNVERIFIED` were not confirmed against live docs.

## Auth
- Bearer token: `Authorization: Bearer KEY...`, base `https://api.telnyx.com/v2`
- Webhook verification is **Ed25519 asymmetric** — headers `telnyx-signature-ed25519` +
  `telnyx-timestamp`; verify `{timestamp}|{raw_body}` against your account public key
  (Portal → Account Settings → Keys & Credentials). Enforce a 5-min timestamp tolerance.
  **This differs from Bandwidth (basic-auth on the callback) — the adapter needs two
  different verify functions.**

## Messaging
- `POST /v2/messages` — `from`, `to`, `text`, `media_urls` (MMS), `subject`
- Number Pool: send with `messaging_profile_id` instead of `from`; Telnyx picks the origin
  number and keeps it sticky per destination where possible
- Alphanumeric sender ID: 3–11 chars, **not valid for US/CA**
- Inbound: `message.received`; shape `{data:{event_type,id,occurred_at,payload,record_type},meta:{attempt,delivered_to}}`
- DLR: `message.sent` (accepted by carrier) then `message.finalized` (terminal)
- Rate limits (⚠UNVERIFIED exact current numbers): account SMS 50/s, MMS 15/s;
  **long code 0.1 MPS per number (~1 msg / 10s)** — carrier-imposed, sizes your number pool
- 10DLC: `POST /v2/10dlc/brand`, `/v2/10dlc/campaign`, `/v2/10dlc/phoneNumberCampaign`.
  ~$4/brand, campaign first 3 months upfront ($6–$30 by use case).
  Mock brands/campaigns exist for sandbox testing.

## Voice — Call Control v2
Imperative: `POST /v2/calls/{call_control_id}/actions/{command}`.
Commands: `dial answer hangup transfer bridge speak playback_start playback_stop gather
gather_using_audio gather_using_speak gather_using_ai record_start record_stop
streaming_start streaming_stop` + conference actions.

Events: `call.initiated call.answered call.bridged call.hangup call.machine.detection.ended
call.ai_gather.ended`. Common fields: `call_control_id call_leg_id call_session_id
client_state connection_id direction from to occurred_at state`.

- **TeXML** = TwiML clone, declarative alternative. Verbs `<Dial> <Gather> <Stream> <Play>`.
- **AMD**: `answering_machine_detection` = `detect | detect_beep | detect_words | premium`.
  Free. Result on `call.machine.detection.ended` → `human | machine | not_sure`
  (treat `not_sure` as human). Premium also detects iOS call screening.
- `gather_using_ai` (one-shot structured JSON-Schema extraction) is **not** the same as
  `ai_assistant_start` (open-ended conversational session). Easy to conflate.

## Real-time media — CONFIRMED BIDIRECTIONAL
`streaming_start` params:
- `stream_url` (required), `stream_track`: `inbound_track|outbound_track|both_tracks`
- `stream_codec`: PCMU/PCMA/G722/OPUS/AMR-WB/**L16**/default  (Telnyx → us)
- `stream_bidirectional_mode`: **`mp3` (default) | `rtp`**  (us → Telnyx)
- `stream_bidirectional_codec`: PCMU (default)/PCMA/G722/OPUS/AMR-WB/L16
- `stream_bidirectional_sampling_rate`: 8000/16000/22050/24000/48000
- `stream_auth_token`, `client_state`, `custom_parameters`

WS events: `connected start media stop mark dtmf error` + client→Telnyx **`clear`**
(halt playback + flush queue — this is the barge-in primitive).
Send audio back as `{"event":"media","media":{"payload":"<base64>"}}`, chunk 20ms–30s.

> **FOOTGUN**: default bidirectional mode is `mp3` — encoding MP3 in a realtime loop adds
> CPU + latency. For a voice agent use `rtp` + `L16` or `PCMU`. L16 is explicitly
> recommended by Telnyx for AI integrations (no transcode on their side).

> ⚠UNVERIFIED: some doc excerpts mention a ~1 media-message/sec client submission ceiling.
> Re-confirm before designing the buffer.

## WebRTC
- `@telnyx/webrtc` (npm, TS) + `@telnyx/react-client`
- Mobile: `@telnyx/react-native-voice-sdk` (current). `@telnyx/react-native` is **deprecated**.
- Auth: SIP credential, **or on-demand credentials per seat**, **or JWT** minted via
  `POST /v2/telephony_credentials/{id}/token` (default 24h). JWT is the right fit for a
  multi-seat browser softphone — no static SIP passwords in the browser.
- Inbound ring + outbound dial with caller-ID selection both supported.
- SIP trunking / BYOC available (`sip.telnyx.com`).

## Numbers
- Search `GET /v2/available_phone_numbers`
- Order `POST /v2/number_orders` (⚠ doc versions disagree on path — confirm against the
  current OpenAPI reference, not `/docs/v1/` or `preview.redoc.ly` mirrors)
- Porting: portability check → draft porting order → LOA + recent invoice → submit
- **Voice config (Connection) and messaging config (Messaging Profile) are two separate
  associations on a number**, not one object.

## SDKs
| Lang | Package | Repo |
|---|---|---|
| Python | `telnyx` | team-telnyx/telnyx-python (184★, sync+async, active) |
| Node/TS | `telnyx` | team-telnyx/telnyx-node |
| Java | maven | team-telnyx/telnyx-java |
| .NET | nuget | team-telnyx/telnyx-dotnet |
| Web RTC | `@telnyx/webrtc` | team-telnyx |

`telnyx-mock` is **deprecated** — mock from the OpenAPI spec (Prism) instead.

## Headstart repos
- **pipecat-ai/pipecat** (~14.7k★) ships `TelnyxFrameSerializer` + `FastAPIWebsocketTransport`
- **pipecat-ai/pipecat-examples** → `telnyx-chatbot/inbound/bot.py` and `outbound/bot.py`
- **livekit/sip** (459★, Go) — SIP↔WebRTC bridge; Telnyx publishes a LiveKit config guide.
  Outbound trunk auth needs a custom `X-Telnyx-Username` SIP header.
- team-telnyx/demo-amd, demo-findme-ivr, telnyx-samples-pwc, telnyx-code-examples, knowledge-base
- **Vocode has no Telnyx integration** — do not budget for it.

## Gotchas
- Webhooks are **at-least-once**, can arrive at both primary and failover URL.
  Dedupe on `data.id`. Must 2xx within ~2s or you get up to 3 retries then failover.
- No general Stripe-style test-mode API key found (⚠UNVERIFIED / likely absent).
  Only 10DLC has mock brands/campaigns. Plan to test on a real low-spend account.

## Architectural implication (IMPORTANT)
**Bandwidth = document-return model** (webhook responds with a BXML document).
**Telnyx = imperative out-of-band command model** (ack the webhook, then POST commands
whenever you like, from any process).

Design the internal interface in the **Telnyx shape** — `event in / command out, async` —
because that model serializes down onto Bandwidth (build BXML as the "dispatch") but the
reverse does not work: a document-return abstraction forces you to hold HTTP responses open
on Telnyx. Consequence: the **Bandwidth adapter is the constrained one** — it can only
express commands that fit "what to do in reply to this one event", so anything mid-stream
(a barge-in `clear`) needs a second round-trip there.
