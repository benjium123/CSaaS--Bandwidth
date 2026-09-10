/**
 * P23a VERIFICATION sweep (Opus supervisor, 2026-09-10).
 *
 * A static probe over the five new assistant surfaces, checking the rules this phase claims to
 * hold — the same fence p20bSettingsSweep and p20cInboxSweep put around their phases:
 *   1. shared primitives only (no raw <button> / <select> / <textarea>, and exactly ONE raw
 *      <input> in the whole directory: KnowledgeTab's file picker, which has no primitive),
 *   2. design tokens only (no raw `neutral-*` palette classes),
 *   3. plain words on every customer-visible string — an AI product is where jargon leaks in, so
 *      LLM / STT / TTS / webhook / carrier / E.164 / registry / endpoint are banned outright,
 *   4. the compliance preamble is shown, and shown read-only.
 *
 * A new hit fails this test. That is the point: it names the file and the words.
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { describe, expect, it } from "vitest";

const DIR = dirname(fileURLToPath(import.meta.url));

const SURFACES = [
  "AiProvidersTab.tsx",
  "AssistantsBuilder.tsx",
  "AssistantConfigTabs.tsx",
  "KnowledgeTab.tsx",
  "SimulatorDrawer.tsx",
];

function source(file: string): string {
  return readFileSync(resolve(DIR, file), "utf8");
}

/** Judges a rule on shipped markup, not on the comments that explain it. */
function withoutComments(src: string): string {
  return src.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
}

describe("P23a assistant surfaces: the files exist", () => {
  it("finds every surface this sweep claims to cover", () => {
    // A renamed or moved file must fail loudly here rather than making the whole sweep vacuous.
    expect(SURFACES.filter((file) => source(file).length > 0)).toHaveLength(SURFACES.length);
  });
});

describe("P23a assistant surfaces: primitives only", () => {
  it.each(SURFACES)("%s has no raw <button>, <select> or <textarea>", (file) => {
    const src = withoutComments(source(file));
    expect(src.match(/<button[\s>]/g) ?? []).toEqual([]);
    expect(src.match(/<select[\s>]/g) ?? []).toEqual([]);
    expect(src.match(/<textarea[\s>]/g) ?? []).toEqual([]);
  });

  it("uses no raw <input> beyond a checkbox and KnowledgeTab's file picker", () => {
    // A raw `<input type="checkbox">` is the repo's long-standing exception (ProvidersPage and
    // the old AgentPage both use one) and there is no file-input primitive either. Every OTHER
    // raw input would be a text field that skipped `Input`, and must fail here.
    const offenders: string[] = [];
    for (const file of SURFACES) {
      for (const tag of withoutComments(source(file)).match(/<input[\s\S]*?\/>/g) ?? []) {
        if (/type="checkbox"/.test(tag)) continue;
        if (file === "KnowledgeTab.tsx" && /type="file"/.test(tag) && /aria-label=/.test(tag)) {
          continue;
        }
        offenders.push(`${file}: ${tag.slice(0, 60)}`);
      }
    }
    expect(offenders).toEqual([]);

    const fileInputs =
      withoutComments(source("KnowledgeTab.tsx")).match(/<input[\s\S]*?type="file"[\s\S]*?\/>/g) ??
      [];
    expect(fileInputs).toHaveLength(1);
  });
});

describe("P23a assistant surfaces: design tokens only", () => {
  it.each(SURFACES)("%s uses no raw neutral-* palette classes", (file) => {
    expect(withoutComments(source(file)).match(/\bneutral-\d{2,3}\b/g) ?? []).toEqual([]);
  });
});

/**
 * The USER-VISIBLE strings only. A source-wide grep would be useless: `type: "webhook"` is a wire
 * value, `kind === "llm"` is a code branch and `["ai-providers"]` is a query key - nobody reads
 * any of them. This pulls literal JSX text nodes (no `{...}` expressions, so backend-supplied
 * values are correctly excluded), the quoted values of the props that end up spoken or hovered,
 * and the `label:` / `description:` / `title:` values of the tool and status catalogues.
 */
function visibleStrings(src: string): string[] {
  const body = withoutComments(src);
  const out: string[] = [];

  for (const match of body.matchAll(/>([^<>{}]+)</g)) {
    const text = match[1].trim();
    // `>` and `<` also appear in `=>`, `a > 0` and generics, so this heuristic picks up
    // stretches of CODE between two of them. Real rendered text is prose on one line:
    // anything carrying code punctuation or a line break is not a string a customer reads.
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

describe("P23a assistant surfaces: plain vocabulary", () => {
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
  ];

  it.each(SURFACES)("%s shows no plumbing vocabulary to the customer", (file) => {
    const hits = visibleStrings(source(file)).filter((text) =>
      WORDS.some(([, re]) => re.test(text)),
    );
    expect(hits).toEqual([]);
  });

  it("says 'language model', 'speech recognition' and 'voice' instead", () => {
    // The three plain words live in api/assistants.ts (AI_KIND_WORDS) and reach the screen
    // through it, so the surfaces must not hard-code a jargon alternative.
    const providers = source("AiProvidersTab.tsx");
    expect(providers).toContain("AI_KIND_WORDS");
  });
});

describe("P23a builder: the compliance preamble is shown and cannot be edited", () => {
  it("renders COMPLIANCE_PREAMBLE read-only in the instructions tab", () => {
    const src = source("AssistantsBuilder.tsx");
    expect(src).toContain("COMPLIANCE_PREAMBLE");
    // The full merged instructions are a preview, never an editable field: if this ever loses
    // readOnly, a customer could edit text the server is going to ignore.
    expect(src).toMatch(/readOnly/);
    // ...and the preamble itself must never be bound to an onChange.
    expect(src).not.toMatch(/value=\{[^}]*COMPLIANCE_PREAMBLE[^}]*\}[\s\S]{0,200}onChange/);
  });
});
