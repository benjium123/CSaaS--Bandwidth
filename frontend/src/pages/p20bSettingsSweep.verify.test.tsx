/**
 * P20b VERIFICATION sweep (Opus verifier, 2026-09-10).
 *
 * A static probe over the nine restyled Settings pages, checking the three design rules
 * the plan states and the VERDICT claims are met:
 *   1. no raw <button> / <select> (primitives only),
 *   2. no raw `neutral-*` Tailwind palette classes (design tokens only),
 *   3. no plumbing vocabulary (carrier / DLR / E.164 / breaker / registry / webhook) in a
 *      user-visible string.
 *
 * Rule 3 has two DOCUMENTED exceptions, encoded below rather than waved away: the word
 * "webhook" in PlatformPage (the surface is literally the developer webhook registry, and
 * PlatformPage.test.tsx pins the strings), and NumberOut.registration_detail, which is
 * backend text rendered as a tooltip. Any NEW hit fails this test - that is the point.
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { describe, expect, it } from "vitest";

const PAGES_DIR = dirname(fileURLToPath(import.meta.url));

const SETTINGS_PAGES = [
  "NumbersPage.tsx",
  "ProvidersPage.tsx",
  "InboxSettingsPage.tsx",
  "QueuesPage.tsx",
  "AppointmentsPage.tsx",
  "PlatformPage.tsx",
  "AgentPage.tsx",
  "FlowsPage.tsx",
  "TeamPage.tsx",
];

function source(file: string): string {
  return readFileSync(resolve(PAGES_DIR, file), "utf8");
}

/** Strips // and block comments so a rule is judged on shipped markup, not on the
 * comments that explain it. */
function withoutComments(src: string): string {
  return src.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
}

describe("P20b settings pages: primitives only", () => {
  it.each(SETTINGS_PAGES)("%s has no raw <button> or <select>", (file) => {
    const src = withoutComments(source(file));
    expect(src.match(/<button[\s>]/g) ?? []).toEqual([]);
    expect(src.match(/<select[\s>]/g) ?? []).toEqual([]);
  });
});

describe("P20b settings pages: design tokens only", () => {
  it.each(SETTINGS_PAGES)("%s uses no raw neutral-* palette classes", (file) => {
    const src = withoutComments(source(file));
    expect(src.match(/\bneutral-\d{2,3}\b/g) ?? []).toEqual([]);
  });
});

/**
 * The USER-VISIBLE strings only. A source-wide grep is useless here: `number.carrier` is a
 * backend field name, `htmlFor="carrier"` is a DOM id, and `["carrier-catalog"]` is a query
 * key - none of them are read by anyone. This pulls literal JSX text nodes (no `{...}`
 * expressions, so backend-supplied values are correctly excluded) plus the quoted values
 * of the props that end up spoken or hovered.
 */
function visibleStrings(src: string): string[] {
  const body = withoutComments(src);
  const out: string[] = [];

  for (const match of body.matchAll(/>([^<>{}]+)</g)) {
    const text = match[1].trim();
    if (text) out.push(text);
  }
  for (const match of body.matchAll(
    /(?:aria-label|title|placeholder|label|ariaLabel|description|emptyText)=\{?"([^"]+)"/g,
  )) {
    out.push(match[1]);
  }
  return out;
}

describe("P20b settings pages: plain vocabulary", () => {
  const WORDS: Array<[string, RegExp]> = [
    ["carrier", /\bcarriers?\b/i],
    ["DLR", /\bDLRs?\b/],
    ["E.164", /E\.?164/],
    ["breaker", /\bbreakers?\b/i],
    ["registry", /\bregistr(y|ies)\b/i],
  ];

  it.each(SETTINGS_PAGES)("%s shows no plumbing vocabulary to the user", (file) => {
    const strings = visibleStrings(source(file));
    const hits = strings.filter((text) => WORDS.some(([, re]) => re.test(text)));
    expect(hits).toEqual([]);
  });

  it("only PlatformPage says 'webhook' to the user (documented exception)", () => {
    const offenders = SETTINGS_PAGES.filter(
      (file) =>
        file !== "PlatformPage.tsx" &&
        visibleStrings(source(file)).some((text) => /webhook/i.test(text)),
    );
    expect(offenders).toEqual([]);
  });
});
