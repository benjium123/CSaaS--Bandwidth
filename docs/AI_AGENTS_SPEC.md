# Ringlite AI Agents — SPEC

Status: draft for approval, 2026-10-06. Supersedes the pricing and template choices in
docs/AI_AGENTS.md (that file keeps the prompt architecture, style rules and template prompts).
Overview doc: https://claude.ai/code/artifact/3e8b7253-7ef7-45a7-acef-be1ed8e9b864

## 1. Goal

Businesses get working voice and SMS agents without writing a prompt. They pick a ready-made
agent or start blank, answer a short interview, see the generated prompt live beside it, test
with a call to their own phone, and go live on a number. Simple, smooth user journey first.

## 2. Decisions (user, 2026-10-06)

| Topic | Decision |
|---|---|
| AI minute price | **$0.35/min all-in**, no credits. Market is ~$0.50-$1.00; $0.15 read as "cheap = bad". |
| Included minutes | **Team: 50/month. Business: 200/month.** No rollover. |
| Starter | Sees the AI agents screen and gallery, locked, with an upgrade prompt. Cannot create or run. |
| Prepaid balance | Auto-recharge **ON by default**: when balance falls to **$5**, charge **$10**. Manual top-up: **$10 minimum, $5 steps**. |
| Builder | **Interview on the left, live prompt on the right.** Both editable (section 4). |
| Voices | **3-4 curated voices** only, not a catalog. |
| Booking | Appointments go to **Google Calendar** or **Ringlite's own calendar**. |
| AI disclosure | On by default. Switch **per workspace and per number**; only Ringlite super admins can change it (ops console, audited). |
| AI Receptionist template | **Dropped.** Everyone ships one; we lead with job-specific agents. |
| Demo | No public demo number. Visitors and users enter **their own number** and the agent calls them ("Call me"). |
| Model | Better than Haiku 4.5. Candidates: Kimi K2.6, DeepSeek V4 Flash, GLM-5.3 Flash (cheap, hosted on GPUs), Gemini Flash if latency wins. Chosen by the bake-off (Phase 0). |
| Outbound AI calls | Wanted (real estate). Ship behind a **consent gate** (section 7). |

## 3. Engine: Telnyx Voice AI vs our LiveKit agent

Verified 2026-10-06 (telnyx.com/pricing/voice-ai-agents): **$0.05/min** platform (STT, TTS incl.
1,400+ voices, orchestration, knowledge base) **+ ~$0.006/min LLM** (Kimi, open models on Telnyx
GPUs) **+ $0.0032/min** call = **~$0.06/min**. The "$0.016" figure was not found.
Telnyx: no bring-your-own Claude/OpenAI key; warm transfer and joining an existing call are not
documented; numbers on SignalWire/Bandwidth need SIP into Telnyx.
Our agent today: ~$0.06-0.08/min, runs on any carrier, joins the LiveKit room (recording,
supervisor, warm transfer), but uses our server CPU (4 cores, ~10-25 calls).

**Plan:** Phase 0 bake-off, same template and script on both, measuring answer latency,
interruption handling, transfer, cost per call. Likely result: Telnyx engine for Telnyx numbers,
our agent for other carriers and for warm transfer. The product (builder, templates, billing)
is engine-neutral: an agent profile carries `engine = livekit | telnyx`.

## 4. Builder: interview + live prompt

Layout: two panes. Left = interview. Right = generated prompt, always visible.

Interview sections (each a form block, never a blank prompt box):
1. Business: name, type, hours, timezone, address, website, service area.
2. Agent: name, voice (one of 3-4), language, greeting.
3. Goal: the one end goal of a conversation (e.g. "a seller agrees to a call with our team").
4. Should do: bullet list.
5. Should never do: bullet list.
6. FAQs: question/answer pairs (also seeded into the knowledge base).
7. Handoff: when to transfer, to which number or teammate.
8. Booking: calendar (Google or Ringlite), slot rules.
9. After the call: summary on/off, fields to extract, outcome codes.

Rules:
- Every interview change regenerates the right pane instantly (deterministic template render, no
  LLM call; empty answers drop their whole sentence).
- Editing the prompt text directly marks it **custom**: the interview stays visible, and a banner
  offers "Regenerate from interview" (overwrites custom edits, with confirm).
- Locked layers (compliance preamble, voice style rules, platform rules) show greyed at the top
  of the prompt pane and are never editable.
- Test panel: simulator (text) and **Call me** (enter your number). Go live needs one passed test.
- Templates = pre-filled interviews. "Start blank" = empty interview.

## 5. Ready-made agents (v1)

| Agent | Channel | Ships |
|---|---|---|
| After-Hours Answering | Voice | v1 |
| Customer Support / FAQ Triage | Voice | v1 |
| Missed-Call Text-Back | SMS | v1 |
| Real Estate Seller Qualifier | Voice + SMS | v1 |
| Appointment Booker | Voice | with calendar tools |
| Real Estate Buyer Qualifier | Voice | with calendar tools |
| Home Services Dispatch | Voice | with calendar tools |
| Restaurant Reservations & Info | Voice | with calendar tools |
| Real Estate Acquisition Negotiator | SMS | section 6 |
| Medical & Dental Front Desk | Voice | only after a BAA chain exists |

## 6. Goal-driven SMS negotiation agent (real estate acquisition)

For the user's own REI CRM first, then as a Ringlite template.
- Context: property record + MLS pull (comps, list history) + full conversation history.
- Goal: get the property under contract within the buyer's price limits (floor/ceiling set per
  lead; the agent never offers above ceiling).
- Behaviour: negotiates over multiple days, follows up on silence, answers objections, keeps
  opt-out (STOP) and quiet hours.
- **Human approval gate:** when the seller agrees, the agent drafts terms and stops; a person
  approves, then the contract is sent (existing contracts flow). Nothing legal is sent unapproved.
- Lives in the REI CRM (rei-crm, separate codebase); its own spec before build.

## 7. Compliance

- AI disclosure in the greeting when on (default). Turning it off is a super-admin action with
  an audit row and a reason; we keep it on for any outbound call.
- Recording notice stays automatic.
- Outbound AI voice calls: FCC (Feb 2024) treats AI voices as "artificial voice" under the TCPA.
  Calls to mobiles need prior express consent, written for marketing. Consent gate: a contact
  must carry `ai_call_consent` (source + timestamp) before an AI outbound dial; opt-in web leads
  and callbacks qualify; purchased lists do not. DNC scrub before every campaign.
- No HIPAA/SOC 2 claims.

## 8. Billing

- AI seconds = agent joined -> left, rounded up per call, capped 4 h. Plan allowance first,
  then $0.35/min from the prepaid balance. AI seconds are not also billed as voice minutes.
- One week shadow billing (record, don't charge), then enforce.
- Balance under $5 -> auto-recharge $10 (if card saved and switch on). Failure -> email + AI
  agents pause at $0 (calls fall back to normal routing, never silence).

## 9. Phases

0. **Bake-off** Telnyx vs LiveKit agent + model choice. Gate: written result, chosen defaults.
A. **Safe and billed**: worker reads /config, one tool registry, SMS preamble, outcome + summary
   posted, AI usage billing at $0.35 with 50/200 allowances, recharge rules, disclosure switch.
   (Done 2026-10-06: SMS booking over-claim fix.) Gate: limits obeyed; ledgers agree in shadow week.
B. **Builder v2**: interview + live prompt, curated voices, gallery (v1 agents), Starter lock,
   Call me on own number. Gate: new user to live agent in under 5 minutes.
C. **Calendar tools**: Google Calendar + Ringlite calendar, check_availability /
   create_appointment, take_message, send_followup_sms, emergency interrupt. Unlocks the rest.
D. **Outbound + consent gate**, QA harness (scripted calls each release).
E. **Negotiation agent** (REI CRM first), analytics.

## 10. Open questions

- Team keeps the $25 add-on, or is AI now included in Team with 50 minutes? (Spec assumes included.)
- Which 3-4 voices (pick by ear in Phase 0)?
- Is the auto-recharge default the same for all workspaces' prepaid balance, or only for AI?
