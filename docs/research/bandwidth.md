# Research — Bandwidth (PRIMARY carrier)

Researched 2026-08-26. `⚠` = unverified, confirm before coding against it.

## Auth
HTTP Basic (`base64(username:password)`) with Dashboard API credentials; `accountId` in the
URL path. Bearer is also supported in places but Basic is the documented default.

## Messaging
`POST https://messaging.bandwidth.com/api/v2/users/{accountId}/messages`
```json
{ "to":["+1..."], "from":"+1...", "text":"...", "media":["https://..."],
  "applicationId":"...", "tag":"...", "priority":"default" }
```
Returns **202 Accepted** — delivery is confirmed by webhook, never by the response.
`applicationId` must be the messaging application bound to the `from` number.
MMS media is a URL array; no binary-in-body upload.

- MMS media: up to **3.75 MB**, free hosted storage ~**48 h**, type inferred from extension.
- Webhooks arrive as a **JSON array**:
  `[{ time, type, to, description, message:{ id, owner, applicationId, time, segmentCount,
  direction, to[], from, text, tag, media[], channel } }]`
  `type` ∈ `message-received | message-sending | message-delivered | message-failed`
  (`message-failed` adds top-level `errorCode`; `message-sending` is MMS-only).
- **Retries for 24 h on any non-2xx, and retries are NOT ordered.** Treat as
  at-least-once + out-of-order. Dedupe on `message.id`. Handlers must be state-based.
- **Rate limits count SEGMENTS, not requests.** Default new account = **1 MPS** per
  number/campaign. A 2-segment message burns 2 units. Over → HTTP 429.
- Error codes are 4-digit: 1st = client(4)/server(5), 2nd = Bandwidth(1)/carrier(7).
  Notables: `4302` bad From · `4403` from-number not messaging-enabled · `4411` MMS too big
  · **`4476` blocked-unregistered (not on a 10DLC campaign)** · `4720` invalid destination
  · `4780` T-Mobile daily volume violation · `5600` carrier queue full.
- Group messaging: HTTP/MM4 numbers only, **not toll-free**, billed per recipient.
- **RCS: A2P at scale is "coming soon". Treat as NOT production-ready.**

## Voice
`POST https://voice.bandwidth.com/api/v2/accounts/{accountId}/calls`
with `from`, `to`, `applicationId`, `answerUrl` → 201 with `callId`, `callUrl`.

**BXML verbs (full list):** `Conference Bridge Pause Forward Transfer Refer Ring Hangup
Redirect PlayAudio SpeakSentence Record StartRecording PauseRecording ResumeRecording
StopRecording Gather StartGather StopGather StartStream StopStream StartTranscription
StopTranscription SendDtmf Tag`

- `Forward` = unanswered inbound only. `Transfer`/`Bridge` = live-call routing.
  `Redirect` = re-point BXML execution to a new URL (not a call transfer).
- `initiate` webhook: `{eventType, eventTime, accountId, applicationId, from, to, direction,
  callId, callUrl, startTime}` (+ optional `uui, diversion, stirShaken, sipCallId, sipHeaders`).
  **Response body must be BXML.**
- `disconnect` webhook adds `cause` ∈ `hangup busy timeout cancel rejected callback-error
  invalid-bxml application-error account-limit node-capacity-exceeded error unknown`.
  **Expects 204 — BXML in the response is IGNORED.**
- **BXML response handling is inconsistent by event type.** `initiate`/answer and
  `ConferenceCreated/Join/Exit` honour BXML; `disconnect`, `streamEventUrl` and
  `ConferenceCompleted` ignore it. Easy place to burn hours.
- AMD: `MachineDetectionConfiguration` — `mode` (`sync`/`async`), `detectionTimeout`,
  `silenceTimeout`, `speechThreshold`, `speechEndThreshold`, `delayResult`.
  Async reports via `machineDetectionComplete` webhook.
- Recording: `transcribe` attr on `<Record>`/`<StartRecording>`, transcripts kept **30 days**.
  `StartTranscription`/`StopTranscription` is a separate *real-time* transcription path.
- **Conference: max 20 participants, max 24 h.** Only 6 verbs legal inside a conference
  context: `PlayAudio SpeakSentence StartRecording StopRecording PauseRecording
  ResumeRecording`. Attributes include `mute`, `hold`, `callIdsToCoach` (whisper/coach).

> **DESIGN CONSTRAINT**: you cannot `StartStream` a conference room. To put an AI agent
> into a multi-party call, start the stream **on the individual leg before it joins**, or
> architect around a bridge call.

## Real-time media — BIDIRECTIONAL, CONFIRMED
`<StartStream>`:
- `mode`: `unidirectional` (default) | **`bidirectional`**
- `tracks`: `inbound | outbound | both`
- Codec: default **PCMU (µ-law) 8 kHz mono**; alt **PCM 8/16/24 kHz, 16-bit LE signed**
- Bandwidth → you: `{"eventType":"start|media|stop","metadata":{accountId,callId,streamId,
  streamName,tracks},"track":"inbound|outbound","payload":"<b64>","sequenceNumber":"..."}`
- You → Bandwidth: `{"eventType":"playAudio","media":{"contentType":"audio/pcmu|audio/pcm",
  "payload":"<b64>"}}` and **`{"eventType":"clear"}` to flush queued audio — this is the
  barge-in primitive.**
- Limits: **max 4 concurrent streams per call**, unique `name` per stream, ≤12
  `<StreamParam/>` per stream. Lifecycle events to `streamEventUrl` (BXML ignored).
- Keeping the call alive during a stream needs `<StopStream wait="true"/>` or `<Pause>` —
  you park the call flow on the stream, it is not a fire-and-forget side channel.
- ⚠ frame size (ms/frame) not documented — measure it.

**Official reference implementation:**
[Bandwidth-Samples/openai-realtime-websockets-python](https://github.com/Bandwidth-Samples/openai-realtime-websockets-python)
— FastAPI, MIT, wires bidirectional `StartStream` ↔ OpenAI Realtime with interruption
handling. This is our protocol reference.

**Official Pipecat serializer:**
[Bandwidth/pipecat-bandwidth](https://github.com/Bandwidth/pipecat-bandwidth) — BSD-2,
first-party, tested against Pipecat 1.4.0. Decodes inbound µ-law 8 k, encodes outbound
µ-law/PCM at 8/16/24 k, terminates via Voice API (OAuth2), **implements barge-in via the
`clear` event**. Two caveats it documents:
1. **DTMF does NOT arrive over the media WS** — handle it via a separate voice webhook.
2. `call_id`/`account_id` must come from the authenticated inbound webhook, **never trusted
   from the WS `start` event.**

## WebRTC / In-App Calling
- ⚠ **Bandwidth's original WebRTC API has been closed to new purchases since May 2023.**
  New customers are pointed at **"In-App Calling"** (JS/Kotlin/Swift SDKs against
  Bandwidth's WebRTC Gateway). Inbound PSTN → browser participant via `<Transfer>` + JWT
  + Participant ID. A "SIP Interconnect" component translates SIP ↔ their WebRTC signaling.
- ⚠ No independent 2026 latency benchmarks for In-App Calling exist. Vendor copy only.
- **BYOC SIP trunking is offered** — you can point your own SIP stack at Bandwidth.

## Numbers
Two generations coexist and **both doc trees are live** — a real integration hazard.
- Legacy **Dashboard/IRIS**: `https://dashboard.bandwidth.com/api/accounts/{accountId}/...`
  → `availableNumbers` (search), `orders` (async), `portins`, `lnpChecker`.
  Site / SipPeer (`SiteId`, `PeerId`, `VoiceProtocol`, `HttpSettings`) is still load-bearing.
- Newer **Numbers API** under `dev.bandwidth.com/docs/numbers/` with its own webhooks,
  port-in/port-out split, standalone LNP checker, toll-free porting validations.
- **Toll-free verification (TFV)** has a dedicated API, no extra cost, webhook on
  approve/deny with reasoning. Separate track from 10DLC.

## 10DLC
Bandwidth is a TCR CSP partner.
- `POST /api/accounts/{accountId}/campaignManagement/10dlc/campaigns` + paginated GET
- Also: Brand Vetting API, Reseller & Brand API, Campaign Imports API
- Prereqs: `10dlcCampaigns` feature flag on the account, API user holds the
  "Campaign Management" role, brand registered before campaigns
- Campaign-management rate limits are **separate** from send limits:
  30 req/min GET (burst 20), 10 req/min PUT/POST/DELETE
- ⚠ **Whether Bandwidth auto-intercepts STOP/HELP server-side is UNVERIFIED.**
  Assume it does not. **We implement keyword handling ourselves.**

## SDKs
Unified `bandwidth-sdk` per language — Python ([Bandwidth/python-sdk](https://github.com/Bandwidth/python-sdk),
active), Node, Java, Ruby, C#, PHP. Covers Voice + BXML builder, Messaging, Conferences,
Numbers lookup, TFV, Media/Recordings/Transcription, MFA.
**Version numbers diverge wildly across languages** (Python 23.x vs Node 6.x) — don't assume parity.
**Deprecated, do not use:** `python-bandwidth`, `node-bandwidth`, `@bandwidth/messaging`,
`@bandwidth/voice`, per-language `*-bandwidth-iris` packages.
No official Go SDK found.

## Sample repos worth reading
- `Bandwidth-Samples/openai-realtime-websockets-python` ← **most valuable**
- `Bandwidth/pipecat-bandwidth` ← **most valuable**
- `Bandwidth-Samples/messaging-send-receive-sms-python` / `-js`
- `Bandwidth-Samples/in-app-calling-demo-python`, `in-app-calling-dialpad-node-react`
- `Bandwidth-Samples/webrtc-hello-world-video-pstn-call-js`, `webrtc-video-meeting-python`
- `Bandwidth/webrtc-sample-conference-node`
- `Bandwidth/bandwidth.github.io`, `Bandwidth/ap-docs` (doc source — grep for examples)

## Gotchas
1. **Two live doc generations.** `dev.bandwidth.com/docs/...` = current.
   `v2.dev.bandwidth.com`, `old.dev.bandwidth.com`, `api.catapult.inetwork.com/v1` = legacy.
   Search results mix them freely. Always confirm the host before implementing.
2. Webhook retries for 24 h, unordered, in parallel with in-flight retries.
3. Callback timeouts are short (e.g. `callbackTimeout` 1–25 s on `<Conference>`).
   A slow handler is treated as failure → retry → duplicate/late BXML application.
4. Segment-based rate limiting produces "inexplicable" 429s if you count API calls.
5. **Sales-gated onboarding historically.** No public pricing. **"Bandwidth Build"
   (launched 2026-06-23)** is their self-serve answer — UI + API + CLI + SDKs + an MCP
   server, trial includes a pre-configured US number and free credits.
   ⚠ Whether the trial scales to production without a sales contract is UNVERIFIED.
   **This is a Phase-0 blocker to confirm.**
6. Provisioning/vetting lead times not published. 10DLC brand/campaign vetting and TFV
   take days industry-wide.
