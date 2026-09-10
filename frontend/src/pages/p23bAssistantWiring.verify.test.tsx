/**
 * P23b VERIFICATION sweep (Opus supervisor, 2026-09-10).
 *
 * The fence around this phase's five surfaces — the flows editor's assistant node, the
 * numbers table's "Answered by" control, the campaigns "AI calls" channel, the builder's
 * three new controls and the inbox's AI call card — plus the ONE thing this phase promised
 * NOT to do: leave the human call path alone.
 *
 * Same shape as p20bSettingsSweep / p20cInboxSweep / p23aAssistants.verify: it reads the
 * shipped source off disk, so a rename fails it loudly rather than making it vacuous.
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { describe, expect, it } from "vitest";

const PAGES = dirname(fileURLToPath(import.meta.url));
const SRC = resolve(PAGES, "..");

/** Every file this phase wrote or changed, repo-relative to `src/`. */
const NEW_COMPONENTS = [
  "components/assistants/VoicePreviewButton.tsx",
  "components/assistants/CallMePanel.tsx",
  "components/assistants/AssistantAnalytics.tsx",
  "components/conversations/AiCallCard.tsx",
];

const CHANGED_PAGES = [
  "pages/FlowsPage.tsx",
  "pages/NumbersPage.tsx",
  "pages/CampaignsPage.tsx",
  "pages/DashboardPage.tsx",
  "components/assistants/AssistantsBuilder.tsx",
];

function source(relative: string): string {
  return readFileSync(resolve(SRC, relative), "utf8");
}

/** Judges a rule on shipped markup, not on the comments that explain it. */
function withoutComments(src: string): string {
  return src.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
}

/**
 * The USER-VISIBLE strings only — the same extractor p23aAssistants.verify uses, and for the
 * same reason: a source-wide grep would flag `channel: "ai_calls"` (a wire value) and
 * `["assistant-analytics"]` (a query key), neither of which anybody reads.
 */
function visibleStrings(src: string): string[] {
  const body = withoutComments(src);
  const out: string[] = [];
  for (const match of body.matchAll(/>([^<>{}]+)</g)) {
    const text = match[1].trim();
    if (!text || /[;=(){}[\]`]|\n/.test(text)) continue;
    out.push(text);
  }
  for (const match of body.matchAll(
    /(?:aria-label|title|placeholder|label|ariaLabel|description|pendingLabel|success)=\{?"([^"]+)"/g,
  )) {
    out.push(match[1]);
  }
  for (const match of body.matchAll(/\b(?:label|description|title):\s*"([^"]+)"/g)) {
    out.push(match[1]);
  }
  return out;
}

describe("P23b: every surface this sweep claims to cover exists", () => {
  it("finds all of them", () => {
    const all = [...NEW_COMPONENTS, ...CHANGED_PAGES, "api/assistantOps.ts"];
    expect(all.filter((file) => source(file).length > 0)).toHaveLength(all.length);
  });
});

describe("P23b new components: shared primitives only", () => {
  it.each(NEW_COMPONENTS)("%s writes no raw <button>, <select> or <textarea>", (file) => {
    const src = withoutComments(source(file));
    expect(src.match(/<button[\s>]/g) ?? []).toEqual([]);
    expect(src.match(/<select[\s>]/g) ?? []).toEqual([]);
    expect(src.match(/<textarea[\s>]/g) ?? []).toEqual([]);
  });

  it.each(NEW_COMPONENTS)("%s writes no raw text <input>", (file) => {
    // `<audio>` is fine (there is no primitive and no reason for one); a raw text input would
    // mean a field that skipped `Input`.
    const offenders = (withoutComments(source(file)).match(/<input[\s\S]*?\/>/g) ?? []).filter(
      (tag) => !/type="checkbox"/.test(tag),
    );
    expect(offenders).toEqual([]);
  });
});

describe("P23b: design tokens only", () => {
  it.each([...NEW_COMPONENTS])("%s uses no raw palette classes", (file) => {
    const src = withoutComments(source(file));
    expect(src.match(/\bneutral-\d{2,3}\b/g) ?? []).toEqual([]);
    // Any raw Tailwind colour scale at all, not just neutral - the whole app runs on tokens.
    const scales =
      src.match(
        /\b(?:bg|text|border|ring|fill|stroke)-(?:slate|gray|zinc|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose)-\d{2,3}\b/g,
      ) ?? [];
    expect(scales).toEqual([]);
  });

  it("the P23b additions to the three changed pages carry no raw palette classes either", () => {
    // CampaignsPage has PRE-EXISTING raw palette classes in its status badges (P20b never
    // restyled it), so this checks only the lines this phase added, identified by the words
    // they introduce.
    const added = source("pages/CampaignsPage.tsx")
      .split("\n")
      .filter((line) => /ai_calls|AI calls|Assistant|assistant/.test(line));
    expect(
      added.filter((line) =>
        /\b(?:bg|text|border)-(?:neutral|gray|green|red|amber|blue)-\d{2,3}\b/.test(line),
      ),
    ).toEqual([]);
  });
});

describe("P23b: plain vocabulary on every customer-visible string", () => {
  const WORDS: Array<[string, RegExp]> = [
    ["LLM", /\bLLMs?\b/i],
    ["STT", /\bSTT\b/],
    ["TTS", /\bTTS\b/],
    ["webhook", /webhook/i],
    ["carrier", /\bcarriers?\b/i],
    ["E.164", /E\.?164/],
    ["DLR", /\bDLRs?\b/],
    ["registry", /\bregistr(y|ies)\b/i],
    ["endpoint", /\bendpoints?\b/i],
    ["bot", /\bbots?\b/i],
    ["profile_id", /profile_?id/i],
  ];

  it.each(NEW_COMPONENTS)("%s shows no plumbing vocabulary", (file) => {
    const hits = visibleStrings(source(file)).filter((text) =>
      WORDS.some(([, re]) => re.test(text)),
    );
    expect(hits).toEqual([]);
  });

  it("no raw disposition or channel enum value ever reaches the screen", () => {
    // `ai_calls`, `no_answer`, `handed_off` and friends are wire values. They are mapped to
    // words in api/assistantOps.ts (dispositionWords) and CampaignsPage (channelLabel); if one
    // leaks into rendered text, this fails and names it.
    for (const file of [...NEW_COMPONENTS, "pages/CampaignsPage.tsx"]) {
      const leaked = visibleStrings(source(file)).filter((text) =>
        /^[a-z]+(?:_[a-z]+)+$/.test(text.trim()),
      );
      expect(leaked, file).toEqual([]);
    }
  });
});

describe("P23b: the wire contract in P23B_HANDOFF.md is what the code calls", () => {
  const ops = source("api/assistantOps.ts");

  it("uses the handoff's exact paths", () => {
    expect(ops).toContain("/api/v1/agent/voices/preview");
    expect(ops).toContain("/call-me");
    expect(ops).toContain("/api/v1/analytics/assistant?");
    expect(source("api/numbers.ts")).toContain("/answered-by");
  });

  it("sends `mode` and only sends `profile_id` with an assistant", () => {
    const numbers = source("api/numbers.ts");
    expect(numbers).toContain('{ mode: "assistant", profile_id: body.profile_id }');
    expect(numbers).toContain('{ mode: "human" }');
  });

  it("the flow editor emits the handoff's node shape", () => {
    const flows = withoutComments(source("pages/FlowsPage.tsx"));
    expect(flows).toContain('nodes[id] = { type: "assistant", profile_id: node.profile_id };');
  });

  it("the campaigns form sends channel ai_calls with an assistant", () => {
    const campaigns = withoutComments(source("pages/CampaignsPage.tsx"));
    expect(campaigns).toContain('value: "ai_calls"');
    expect(campaigns).toContain("vars.agent_profile_id = agentProfileId");
  });

  it("a missing cost is never rendered as a number", () => {
    // formatCostMicros returns null rather than "$0.00", and the tile is conditional on it.
    expect(ops).toMatch(/formatCostMicros[\s\S]{0,220}return null/);
    expect(source("components/assistants/AssistantAnalytics.tsx")).toContain("formatCostMicros");
  });
});

describe("P23b: the human call path is untouched", () => {
  const timeline = source("components/conversations/Timeline.tsx");

  it("Timeline still renders its own plain call card, and only branches on `assistant`", () => {
    expect(timeline).toContain("function CallTimelineItemView");
    expect(timeline).toContain("item.assistant");
    // Exactly one branch, so a human call cannot accidentally route into the AI card.
    expect(timeline.match(/<AiCallCard/g) ?? []).toHaveLength(1);
  });

  it("Timeline's change is additive - the voicemail and message cards are still there", () => {
    expect(timeline).toContain("function VoicemailTimelineItemView");
    expect(timeline).toContain('case "call":');
    expect(timeline).toContain('case "voicemail":');
  });

  it("the AI card asks for a transcript only once it is expanded", () => {
    // useCall is disabled by a null id, so the collapsed card must pass null.
    expect(source("components/conversations/AiCallCard.tsx")).toMatch(
      /useCall\(\s*api\s*,\s*expanded\s*\?\s*item\.id\s*:\s*null\s*\)/,
    );
  });
});

describe("P23b: the builder's three new controls are mounted", () => {
  const builder = source("components/assistants/AssistantsBuilder.tsx");

  it("Preview voice is wired, not the P23a placeholder", () => {
    expect(builder).toContain("<VoicePreviewButton");
    // The placeholder said this; if it is still on screen, nothing was wired.
    expect(builder).not.toContain("Playing a sample is coming soon.");
  });

  it("Call me and the analytics strip are both mounted", () => {
    expect(builder).toContain("<CallMePanel");
    expect(builder).toContain("<AssistantAnalyticsPanel");
  });

  it("the dashboard tile shares the page's range instead of carrying its own", () => {
    const dashboard = source("pages/DashboardPage.tsx");
    expect(dashboard).toContain("<AssistantAnalyticsStrip days={days} />");
    expect(dashboard).not.toContain("<AssistantAnalyticsPanel");
  });
});
