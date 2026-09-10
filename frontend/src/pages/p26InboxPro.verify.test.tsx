/**
 * P26 VERIFICATION sweep (Opus supervisor, 2026-09-10).
 *
 * The fence around the four drafted runs. Two kinds of check:
 *   1. static probes over the files P26 adds or rewrites - the palette rule on the one
 *      new surface the existing sweeps do not reach (components/shell/NotificationBell),
 *      the "no separate templates button" rule the composer's "/" menu replaces, and the
 *      wiring that is easy to drop in a later refactor (the bell mounted on the rail, the
 *      composer given a thread to note on, the timeline able to render a note);
 *   2. one runtime check of the chip rule, because "more than five chips collapse" is a
 *      product rule, not a string.
 *
 * The existing sweeps still own what they owned: p20cInboxSweep covers every .tsx under
 * components/inbox and components/conversations (including this phase's new SlaChip and
 * the two new test files), and p20bSettingsSweep covers pages/InboxSettingsPage.tsx.
 * Nothing here weakens or repoints either of them.
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { describe, expect, it } from "vitest";

import { FILTER_CHIPS, MAX_VISIBLE_CHIPS } from "@/components/conversations/ConversationList";
import { SNOOZE_PRESETS, slaState, parseLocalDateTime } from "@/api/inboxPro";

const PAGES_DIR = dirname(fileURLToPath(import.meta.url));
const SRC = resolve(PAGES_DIR, "..");

function withoutComments(src: string): string {
  return src.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
}

function source(rel: string): string {
  return withoutComments(readFileSync(resolve(SRC, rel), "utf8"));
}

/** Every P26 file that no other sweep reaches. */
const SHELL_FILES = ["components/shell/NotificationBell.tsx"];

describe("P26: the one new surface obeys the palette rules", () => {
  it.each(SHELL_FILES)("%s uses no raw Tailwind colour scale", (rel) => {
    const hits = (
      source(rel).match(
        /\b(?:red|green|blue|yellow|orange|lime|teal|cyan|sky|indigo|violet|purple|fuchsia|pink|rose|amber|emerald|gray|zinc|slate|stone|neutral)-\d{2,3}\b/g,
      ) ?? []
    ).filter((hit) => !/^(amber|emerald)-\d{2,3}$/.test(hit));
    expect(hits).toEqual([]);
  });

  it.each(SHELL_FILES)("%s has no raw <button>, <select> or <input>", (rel) => {
    const src = source(rel);
    expect(src.match(/<button[\s>]/g) ?? []).toEqual([]);
    expect(src.match(/<select[\s>]/g) ?? []).toEqual([]);
    expect(src.match(/<input[\s>]/g) ?? []).toEqual([]);
  });
});

describe("P26: plain words", () => {
  /** "SLA" is the internal name for the reply/resolve times. It must never reach a
   * screen: the customer sees "First reply within (minutes)", "Overdue", "Reply time".
   * The identifiers on the wire (sla_first_response_minutes, the `sla` field) are fine -
   * this checks rendered text and the props that get spoken or hovered. */
  const SURFACES = [
    "components/shell/NotificationBell.tsx",
    "components/conversations/SlaChip.tsx",
    "components/conversations/ConversationHeader.tsx",
    "components/conversations/ConversationList.tsx",
    "components/conversations/InboxColumn.tsx",
    "components/inbox/Composer.tsx",
    "pages/InboxSettingsPage.tsx",
  ];

  function visibleStrings(src: string): string[] {
    const out: string[] = [];
    for (const match of src.matchAll(/>([^<>{}]+)</g)) {
      const text = match[1].trim();
      // Discard candidates carrying code punctuation or a newline: `=> <` and multi-line
      // JSX expressions both match the crude >...< heuristic (P23a defect F13).
      if (!text || /[\n{}=;]/.test(text)) continue;
      out.push(text);
    }
    for (const match of src.matchAll(
      /(?:aria-label|title|placeholder|label|description)=\{?["`]([^"`]+)["`]/g,
    )) {
      out.push(match[1]);
    }
    return out;
  }

  it.each(SURFACES)("%s says none of SLA / webhook / E.164 / thread id", (rel) => {
    const hits = visibleStrings(source(rel)).filter((text) =>
      /\bSLA\b|webhook|E\.164|thread id/i.test(text),
    );
    expect(hits).toEqual([]);
  });
});

describe("P26: the composer's only way to a saved reply is '/'", () => {
  const composer = () => source("components/inbox/Composer.tsx");

  it("has no separate templates button", () => {
    // The rule is "typing / is the way in" - a button would be a second, competing one.
    const src = composer();
    // The ONE documented exception is the quick-pick list's own name - it is the
    // feature, not a second way in. Everything else naming a template is a button.
    const named = (
      src.match(/(?:aria-label|title)=\{?["`][^"`]*(?:template|saved repl)[^"`]*["`]/gi) ?? []
    ).filter((hit) => !hit.includes("Insert a saved reply"));
    expect(named).toEqual([]);
    // Same guard the plain-words sweep uses: the crude `>...<` heuristic also matches
    // arrow functions and multi-line JSX expressions, which are code, not screen text.
    const textNodes = (src.match(/>([^<>{}]+)</g) ?? [])
      .map((match) => match.slice(1, -1).trim())
      .filter((text) => text && !/[\n{}=;]/.test(text))
      .filter((text) => /template/i.test(text));
    expect(textNodes).toEqual([]);
  });

  it("does open a saved-reply list and searches for it", () => {
    // The mirror of the test above: prove the feature exists, so "no button" cannot be
    // satisfied by having no quick-pick at all.
    const src = composer();
    expect(src).toContain("fetchTemplates");
    expect(src).toMatch(/Insert a saved reply/);
  });

  it("keeps one Reply | Note toggle, and only one", () => {
    const src = composer();
    expect((src.match(/role="tablist"/g) ?? []).length).toBe(1);
    expect(src).toContain("postThreadNote");
  });
});

describe("P26: the wiring that is easy to lose", () => {
  it("the rail mounts the bell", () => {
    expect(source("components/shell/Sidebar.tsx")).toContain("<NotificationBell />");
  });

  it("the inbox page gives the composer a conversation to note on", () => {
    expect(source("pages/ConversationsPage.tsx")).toMatch(/threadId=\{/);
  });

  it("the timeline renders a note", () => {
    const src = source("components/conversations/Timeline.tsx");
    expect(src).toContain('case "note"');
    expect(src).toMatch(/NoteTimelineItemView/);
  });

  it("the thread header can snooze", () => {
    const src = source("components/conversations/ConversationHeader.tsx");
    expect(src).toContain("snoozeThread");
    expect(src).toContain("unsnoozeThread");
  });

  it("the inbox column offers Snoozed and Overdue", () => {
    const src = source("components/conversations/InboxColumn.tsx");
    expect(src).toContain('"snoozed"');
    expect(src).toContain('"overdue"');
  });
});

describe("P26: product rules", () => {
  it("collapses the filter row by default - Fable dropped the limit below five", () => {
    // Not a style preference: past MAX_VISIBLE_CHIPS, a row of chips stops reading as a
    // set of choices. Fable's call was to ship the collapsed dropdown live rather than
    // dormant, so the five stock chips (Unread, Important, Unresponded, Snoozed,
    // Overdue) must already exceed the limit - not sit exactly at it.
    expect(MAX_VISIBLE_CHIPS).toBe(4);
    expect(FILTER_CHIPS.length).toBeGreaterThan(MAX_VISIBLE_CHIPS);
  });

  it("offers exactly the four snooze presets, in order", () => {
    expect(SNOOZE_PRESETS.map((preset) => preset.label)).toEqual([
      "In 1 hour",
      "In 3 hours",
      "Tomorrow at 9am",
      "Next week",
    ]);
  });

  it("counts down and calls a breach Overdue", () => {
    const now = new Date("2026-09-10T12:00:00Z");
    // `label` only exists on the two non-"none" arms, so read it through a narrowing
    // helper rather than casting the union away.
    const labelOf = (state: ReturnType<typeof slaState>): string | null =>
      state.kind === "none" ? null : state.label;

    expect(slaState(null, now)).toEqual({ kind: "none" });
    expect(labelOf(slaState({ due_at: "2026-09-10T12:45:00Z", breached: false }, now))).toBe(
      "45m left",
    );
    expect(slaState({ due_at: "2026-09-10T11:00:00Z", breached: false }, now).kind).toBe(
      "overdue",
    );
    expect(labelOf(slaState({ due_at: null, breached: true }, now))).toBe("Overdue");
  });

  it("refuses a snooze time that is not in the future", () => {
    const now = new Date("2026-09-10T12:00:00Z");
    expect(parseLocalDateTime("", now)).toBeNull();
    expect(parseLocalDateTime("not a date", now)).toBeNull();
    expect(parseLocalDateTime("2026-09-09T12:00", now)).toBeNull();
    expect(parseLocalDateTime("2027-09-09T12:00", now)).not.toBeNull();
  });
});
