/**
 * P20c VERIFICATION sweep (Opus supervisor, 2026-09-10).
 *
 * P20b left the app with TWO colour systems: the Settings pages ran on the CSS-variable
 * design tokens while the whole Inbox surface still ran on P16's raw `neutral-*` Tailwind
 * scale (P20b VERDICT open item 3). P20c item 3 moved the Inbox onto the same tokens.
 * This is the regression fence: a static probe over every source file of the three inbox
 * directories, asserting that no raw palette class comes back.
 *
 * It deliberately mirrors p20bSettingsSweep.verify.test.tsx (same comment-stripping, same
 * "documented exceptions are encoded, not waved away" rule) so the two sweeps stay
 * readable side by side.
 */
import { readdirSync, readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { describe, expect, it } from "vitest";

const PAGES_DIR = dirname(fileURLToPath(import.meta.url));
const CONVERSATIONS_DIR = resolve(PAGES_DIR, "../components/conversations");
const INBOX_DIR = resolve(PAGES_DIR, "../components/inbox");

/** Every .tsx under the three inbox surfaces, tests included - a test file has no more
 * business hard-coding a palette class than a component does. */
const FILES: Array<[label: string, path: string]> = [
  ["pages/ConversationsPage.tsx", resolve(PAGES_DIR, "ConversationsPage.tsx")],
  ...readdirSync(CONVERSATIONS_DIR)
    .filter((name) => name.endsWith(".tsx"))
    .map((name): [string, string] => [
      `components/conversations/${name}`,
      resolve(CONVERSATIONS_DIR, name),
    ]),
  ...readdirSync(INBOX_DIR)
    .filter((name) => name.endsWith(".tsx"))
    .map((name): [string, string] => [`components/inbox/${name}`, resolve(INBOX_DIR, name)]),
];

/** Strips // and block comments so a rule is judged on shipped markup, not on the
 * comments that explain it (this file's own mapping notes included). */
function withoutComments(src: string): string {
  return src.replace(/\/\*[\s\S]*?\*\//g, "").replace(/^\s*\/\/.*$/gm, "");
}

function source(path: string): string {
  return withoutComments(readFileSync(path, "utf8"));
}

describe("P20c inbox surface: design tokens only", () => {
  it("finds the three inbox directories", () => {
    // A typo in a path above would make every test below vacuously pass.
    expect(FILES.length).toBeGreaterThanOrEqual(10);
    expect(FILES.map(([label]) => label)).toContain("pages/ConversationsPage.tsx");
  });

  it.each(FILES)("%s uses no raw neutral-* palette classes", (_label, path) => {
    expect(source(path).match(/\bneutral-\d{2,3}\b/g) ?? []).toEqual([]);
  });

  /**
   * The wider sweep. `amber-400` (the Important star, in two files) and `emerald-400`
   * (the contact panel's transient "Saved" flash) are the two DOCUMENTED exceptions:
   * they are semantic status colours with no token to move to, and inventing one was out
   * of scope for a mechanical swap. Everything else - including the `red-400` the inbox
   * used for errors, which is now `text-destructive` - must be a token.
   */
  const ALLOWED = /^(amber|emerald)-\d{2,3}$/;

  it.each(FILES)("%s uses no other raw Tailwind colour scale", (_label, path) => {
    const hits = (
      source(path).match(
        /\b(?:red|green|blue|yellow|orange|lime|teal|cyan|sky|indigo|violet|purple|fuchsia|pink|rose|amber|emerald|gray|zinc|slate|stone|neutral)-\d{2,3}\b/g,
      ) ?? []
    ).filter((hit) => !ALLOWED.test(hit));
    expect(hits).toEqual([]);
  });
});
