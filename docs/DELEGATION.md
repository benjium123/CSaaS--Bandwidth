# Delegation map for this project

Follows `~/.claude/rules/delegation.md`. This file says which tier does what **on this
codebase**, so a new session doesn't have to re-derive it.

## Tiers

| Tier | Model | Role here |
|---|---|---|
| 1 | **Fable 5** | Phase plans, test specs, guardrails, architecture, DB schema, compliance logic, audio-pipeline changes, security review, final sign-off. **Never delegated.** |
| 2 | **Opus 5** | Reviews implementer test results → approve / precise fix / escalate. Code review of delegated output. Multi-step debugging that isn't architectural. |
| 3 | **DeepSeek V4 Pro** (`delegate.py deepseek`) | **Primary implementer** while OpenAI balance is on hold. Heavy codegen, CRUD, adapters, long-file transforms. |
| 3 | **Sonnet 5** (`Agent model: sonnet`) | In-repo multi-file mechanical edits where external delegation can't run tools locally. |
| 4 | **Haiku 4.5** / **DeepSeek V4 Flash** | Reading, summarizing, research, docs, boilerplate unit tests, log digestion. |

⚠ **GPT 5.6 Terra / Luna are ON HOLD** — no OpenAI balance. Route to DeepSeek V4 Pro.

## DeepSeek model IDs — verified, do not pin a date

`GET https://api.deepseek.com/models` on **2026-08-26** returns exactly three IDs:

```
deepseek-v4-pro
deepseek-v4-flash
deepseek-v4-flash-vision-exp
```

**These undated aliases are the only callable IDs.** Dated build labels seen in changelogs
(`0731`, `0813`, `0831`, …) are the builds *behind* these aliases — they are **not** model
IDs and passing one returns 404. The alias always resolves to the newest build, so pinning a
date would be both broken and, if it worked, older.

`delegate.py` already uses the correct undated aliases (`deepseek-v4-pro`,
`deepseek-v4-flash`, `deepseek-v4-flash-vision-exp`). No script change needed when DeepSeek
ships a new build.

Legacy names `deepseek-chat` and `deepseek-reasoner` are sunset — **never use them.**

In this repo the aliases are surfaced as `DEEPSEEK_MODEL_PRO`, `DEEPSEEK_MODEL_FLASH`,
`DEEPSEEK_MODEL_VISION` in `.env`.

## Per-phase routing

| Phase | Fable owns (never delegate) | Delegate to implementer | Notes |
|---|---|---|---|
| P0 | RBAC + multi-tenant schema, settings validation design | scaffold fork, Docker Compose, CI, deploy script | Schema is Tier-1 — money/tenancy boundary |
| P1 | CAL interface design, idempotency + state-machine design | Bandwidth messaging adapter, webhook routes, models | The interface shape is D2 — Fable's call |
| P2 | thread/contact data model | React inbox, CRUD endpoints | Good DeepSeek Pro work |
| P3 | **compliance gate design, consent ledger schema** | media pipeline, templates, opt-out keyword engine | Compliance logic reviewed by Fable line-by-line |
| P4 | 10DLC/TFV workflow design | number search/order/port endpoints, inventory UI | |
| P5 | **call + leg state machine** | BXML builder, recording, call log UI | Leg state machine is where transfers break — Tier 1 |
| P6 | softphone transport spike + decision | softphone UI, caller-ID picker | Spike is Fable; UI is delegated |
| P7 | **entire phase — audio pipeline** | latency harness scaffolding, test fixtures only | **Audio is Tier-1. Do not delegate frame handling, pacing, or barge-in.** |
| P8 | **VAD/endpointing tuning, barge-in wiring** | STT/TTS adapter plumbing, transcript storage | Same rule as P7 |
| P9 | tool-calling contract, warm-handoff context design | individual tool implementations | |
| P10 | brain/state-machine design, guardrails | SMS agent plumbing, memory store | |
| P11 | **pacing algorithm, abandon-rate math, compliance enforcement** | import parser, campaign CRUD, dialer UI | Predictive pacing touches TCPA — Tier 1 |
| P12 | routing/queue semantics | IVR builder UI, hold music, voicemail | |
| P13 | metering correctness, API key scoping | dashboards, charts, webhook delivery | Billing math is Tier 1 |
| P14 | **failover policy, security review** | Telnyx adapters, load test harness | |

## Standing rules for this repo

**Always delegate:**
- Reading/summarizing any file → Haiku or `delegate.py auto`
- Carrier API research → Haiku subagent with WebSearch
- Adapter boilerplate, CRUD, React forms → `delegate.py deepseek`
- Docs, unit-test scaffolding, reformatting → `delegate.py auto`

**Never delegate (Fable direct):**
- Anything in the audio path (WS-4 frame handling, pacing, barge-in, VAD)
- Compliance logic (WS-8) — consent, opt-out, quiet hours, DNC, recording consent
- DB schema and migrations
- The CAL interface shape
- Dialer pacing / abandon-rate math
- Security review, final phase sign-off
- Any fix after a guardrail violation or a second failed attempt

**Implementer guardrail template** — every handoff must include:
```
ALLOWED FILES:   <exact paths>
FORBIDDEN:       alembic/versions/*, .env*, docker-compose.prod.yml,
                 anything under app/media/ or app/compliance/,
                 any file not listed above
SCOPE:           max N files, no new dependencies without approval,
                 no signature changes to functions used outside scope
```

A forbidden-file touch = discard the changes, Fable fixes directly, **no retry.**
A logic error = Opus writes fix instructions, **one retry**, then escalate to Fable.

## Delegate script
```bash
python C:\Users\omer_\claude_tools\delegate.py "task" "filepath" deepseek
# tiers: auto (Flash) | deepseek (V4 Pro) | deepseek-flash | deepseek-vision | gemini | kimi
```
Keys are in `C:\Users\omer_\claude_tools\.env` (auto-loaded). Present today:
`OPENAI_API_KEY`, `OPENAI_MODEL`, `OPENAI_LIGHT_MODEL`, `DEEPSEEK_API_KEY`.
