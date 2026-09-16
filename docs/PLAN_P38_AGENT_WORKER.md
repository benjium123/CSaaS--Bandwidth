# P38 — AI worker goes live, media plane gets headroom

Written 2026-09-16 after the "what can we use from the carrier SDKs" review. Decision: **no
new SDK, no LiveKit Cloud, no second softphone.** LiveKit is already self-hosted on the VPS
(livekit-server v1.8 + livekit-sip v1.8, Docker, host networking). The two things actually
standing between us and concurrent AI calling are both on our side of the fence:

1. **The AI worker has never been deployed.** `agents/` ships with the repo, `RUNBOOK.md`
   says so ("nothing runs it yet"), and `docker ps` on the box confirms it. P23b marked the
   *API side* deployed 2026-09-10; the worker that actually talks is not running anywhere.
   Even if it were, it could not get a job: the worker registers as `agent_name="ai"`
   (`agents/ai_agent.py:934`) while the backend dispatches to `"ai-agent"`
   (`services/assistant_dispatch.py:25`, `AI_AGENT_NAME` unset on the box). And every AI
   key in `/opt/csaas/.env` is empty (Deepgram, ElevenLabs, Anthropic, OpenAI - checked
   2026-09-16, lengths only).
2. **The first hard concurrency wall is a port range.** `sip.yaml` gives the SIP bridge 500
   RTP ports (10000-10499) and `livekit.yaml` gives the SFU 500 (50700-51199). A phone call
   takes a port pair on the SIP side, so roughly **250 concurrent calls** and then new calls
   fail. Nothing else on the box listens anywhere near those ranges (scanned 2026-09-16:
   only 5060/udp in use; 10500-11999 and 51200-52699 are empty).

Everything else - CPU, RAM, bandwidth - has headroom: 8 cores / 24 GB, other tenants use
~3 cores, LiveKit and the SIP bridge idle under 1%. Human calls barely load the box. AI
calls are the heavy ones (roughly 10-25 concurrent per 4 cores / 8 GB by LiveKit's own
guidance), which is why the worker gets a CPU/memory cap below and why the NEXT phase after
this one is "a second VPS for workers", not "a bigger LiveKit".

## Goal
Production AI calling works end to end on the existing box, and the media plane can carry
~1,000 concurrent calls before a config change is needed again. No new recurring cost.

## Pre-dependencies (operator, wall-clock - nothing in this phase works without them)
- **AI keys into `/opt/csaas/.env`**: `DEEPGRAM_API_KEY`, `ELEVENLABS_API_KEY`,
  `ELEVENLABS_VOICE_ID`, `ANTHROPIC_API_KEY`. The worker's default LLM path is Anthropic
  `claude-haiku-4-5`; `OPENAI_API_KEY` only if `LLM_PROVIDER=openai` is wanted. I never
  write `.env`; the operator does.
- **Telnyx SIP connection concurrent-channel limit**: read it in the Telnyx portal (Voice →
  SIP Trunking → the FQDN connection → Inbound/Outbound settings). If it is below ~50, raise
  it there. Not readable from the box (no Telnyx key in the csaas `.env`).
- **ufw** (operator runs; `deploy.sh` does not manage the firewall):
  ```bash
  ufw allow 51200:52699/udp comment 'csaas livekit rtp (P38 widen)'
  ufw allow 10500:11999/udp comment 'csaas sip rtp (P38 widen)'
  ```
  Verified free on 2026-09-16 - re-check with `ss -lun` right before adding.

## Allowed files (implementer may read and write)
- `agents/ai_agent.py` — ONE change: `agent_name=os.getenv("AI_AGENT_NAME", "ai-agent")`
  so the default matches the backend's `AI_AGENT_NAME_DEFAULT` and one env var sets both.
- `agents/tests/test_ai_agent_sdk.py` — tests for the name resolution.
- `deploy/Dockerfile.agent` — NEW. Worker image.
- `deploy/livekit/docker-compose.livekit.yml` — add the `agent` service (media plane file,
  the worker is part of the media plane by D17).
- `deploy/livekit/livekit.yaml.tpl` — `port_range_end: 51199` → `52699`.
- `deploy/livekit/sip.yaml.tpl` — `rtp_port.end: 10499` → `11999`.
- `deploy/livekit/README.md` — firewall block + a "5. AI worker" section.
- `deploy/deploy.sh` — only if it does not already `--build` every service in both compose
  files; verify first, change nothing if it does.
- `docs/RUNBOOK.md`, `docs/ROADMAP.md` (row 21), `docs/OPEN_ISSUES.md` (D82 below).

## Forbidden
- `backend/**` (no API change is needed; the dispatch name is fixed on the worker side)
- `backend/migrations/**`
- `.env` / any secrets, `deploy/nginx-*.conf` (shared with other tenants), ufw
- anything outside `agents/`, `deploy/`, `docs/`

## Implementation notes

### Worker image (`deploy/Dockerfile.agent`)
- `python:3.12-slim`, same shape as `deploy/Dockerfile`: non-root uid 10001, bounded
  layers, `PYTHONUNBUFFERED=1`.
- Install `agents/requirements.txt` (its own dependency set ON PURPOSE - livekit-agents must
  never enter the API image, phase-7-plan).
- Copy `agents/` to `/app/agents` so the module path is `agents.ai_agent`.
- **At build time** run `python -m agents.ai_agent download-files`. This fetches the Silero
  VAD and turn-detector model weights into the image; without it the FIRST call on every
  fresh container stalls while models download.
- `CMD ["python", "-m", "agents.ai_agent", "start"]` (`dev` is for laptops: hot reload,
  verbose).

### Compose service (`deploy/livekit/docker-compose.livekit.yml`)
```yaml
  agent:
    build:
      context: ..            # repo root, like the api image
      dockerfile: deploy/Dockerfile.agent
    restart: unless-stopped
    env_file: [../.env]
    environment:
      # Host networking: compose DNS names ("livekit", "api") do not resolve here, and
      # .env's LIVEKIT_URL=ws://livekit:7880 would fail. Loopback reaches both because
      # livekit is host-networked and api publishes 127.0.0.1:8080.
      LIVEKIT_URL: ws://127.0.0.1:7880
      LIVEKIT_API_KEY: csaas-media
      BACKEND_URL: http://127.0.0.1:8080
      AI_AGENT_NAME: ${AI_AGENT_NAME:-ai-agent}
    network_mode: host
    # The box is shared with other businesses. A worker under load is CPU-bound (STT/TTS
    # streaming, VAD); cap it so a burst of AI calls degrades AI calls, not the CRM.
    cpus: "4"
    mem_limit: 8g
    depends_on: [livekit]
    logging: *default-logging
```
`environment:` overrides `env_file` for the same key in Compose, which is what makes the
loopback URLs win over `.env`'s `ws://livekit:7880`. `LIVEKIT_API_SECRET` comes from `.env`
untouched (the worker reads `LIVEKIT_API_KEY`/`LIVEKIT_API_SECRET`, `agents/ai_agent.py:51`).

### Name fix (`agents/ai_agent.py`)
`agent_name="ai"` → `agent_name=os.getenv("AI_AGENT_NAME", "ai-agent")`. Log the resolved
name once at startup so a mismatch is visible in `docker logs` instead of manifesting as
"AI never answers".

### Ports
Widen both ranges to 2,000 ports: SIP RTP 10000-11999 (~1,000 concurrent calls), SFU RTC
50700-52699. Config changes need `docker compose ... restart livekit livekit-sip` - a brief
drop of any live call, so restart off-hours. Update the README firewall block to match.

### What this phase deliberately does NOT do
- Not a second worker box (next phase, when concurrent AI calls pass ~10).
- Not recording (`egress` is not deployed; separate slice).
- Not the SignalWire second trunk (P39: per-carrier trunk id in `voice_plane`).
- Not per-org AI keys (`AI_PER_ORG_KEYS` stays off; env keys are the path today).

## Test spec
Unit (offline, `agents` venv, `pytest agents/tests`):
- [ ] `test_agent_name_defaults_to_the_backends_dispatch_name` → with `AI_AGENT_NAME` unset,
      the WorkerOptions agent_name is the literal `"ai-agent"` (the agents package cannot
      import the backend; pin the literal and say why in the test).
- [ ] `test_agent_name_env_override` → `AI_AGENT_NAME=foo` → `"foo"`.
- [ ] every existing `agents/tests/*` still green.

Build:
- [ ] `docker build -f deploy/Dockerfile.agent .` succeeds; `docker run --rm <img> python -m
      agents.ai_agent --help` prints the livekit-agents CLI; the image contains the
      downloaded model files (no network needed at first call).
- [ ] `deploy.sh`'s compose invocation builds the new service (verify, do not assume).

Integration (on the box, after deploy + keys + ufw):
- [ ] `docker logs csaas-agent-1` shows the worker registered with `agent_name=ai-agent`.
- [ ] One outbound AI call to the operator's own phone via the API: `call_answered`, the AI
      speaks within 2 s of answer, hangup writes the `/agent/outcome` row.
- [ ] Conversation-replay gate (`agents/replay_harness.py`, README): rt ≥ 0.97, zero
      underruns, `tail_energy_ratio ≥ 0.5`. **Any audio-path change must pass this** (D5).
- [ ] After `restart livekit livekit-sip`: a test call's RTP binds inside the new ranges
      (`ss -lun` during the call) and `ufw status` shows both new rules.
- [ ] `docker stats`: the worker's CPU stays under its 4-core cap during the test call and
      the other tenants' containers are unaffected.

Manual:
- [ ] Operator confirms the Telnyx channel limit value and records it in RUNBOOK.

Pass criteria: all boxes ticked. Commit only after the unit + build items are green; the
integration items gate the *deploy*, not the commit.

## Deploy
yes. Order matters:
1. Operator: keys into `.env`, ufw rules, Telnyx channel limit checked.
2. `bash deploy/deploy.sh` (builds + starts the worker alongside the existing services).
3. `docker compose -f deploy/docker-compose.prod.yml -f deploy/livekit/docker-compose.livekit.yml restart livekit livekit-sip` (port ranges).
4. Integration checks above.

Rollback: remove the `agent` service and `compose up -d` (worker gone, nothing else
changed); revert the two `.tpl` ranges and restart the two media services.

## Open issue to log
- **D82** — worker `agent_name="ai"` vs backend `"ai-agent"`: the AI worker could never
  have received a dispatch. Found 2026-09-16 while planning P38; fixed here.

## Cost reference (for the operator, from the 2026-09-16 pricing pull)
Per minute, self-hosted LiveKit (no platform fee): human call $0.005 (carrier only); AI call
$0.081 on the default Anthropic path (carrier $0.005 + Deepgram $0.0077 + Haiku 4.5 $0.009
+ ElevenLabs Flash $0.045 + LiveKit $0), or $0.073 with GPT-4o-mini. SignalWire's hosted AI
would be $0.168. The ElevenLabs voice is the single biggest lever if AI cost ever matters.
