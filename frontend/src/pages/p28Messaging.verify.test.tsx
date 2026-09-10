/**
 * P28 verifier probes (Opus).
 *
 * Three claims the behavioural tests can only check one instance of, and which would be
 * quietly wrong if a later edit moved them:
 *  - the client's attachment ceiling is the SERVER's constant, not a nearby round number
 *  - the composer never promises link tracking outside the toggle's own branch
 *  - the bubble shows the public failure sentence and never the provider's code
 */
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { MAX_MEDIA_BYTES, attachmentLimitSentence } from "@/api/messaging";

const here = dirname(fileURLToPath(import.meta.url));

function source(relative: string): string {
  return readFileSync(resolve(here, "..", relative), "utf8");
}

describe("P28: the attachment ceiling is the server's own number", () => {
  it("matches backend/app/services/media.py MAX_MEDIA_BYTES", () => {
    expect(MAX_MEDIA_BYTES).toBe(3_750_000);
  });

  it("never advertises more than it would accept", () => {
    const advertised = Number(/([\d.]+) MB/.exec(attachmentLimitSentence())?.[1]);
    expect(advertised).toBeGreaterThan(0);
    // Floored, never rounded: advertising 3.8 MB and then refusing at 3.75 would be the
    // one way this sentence could actively mislead.
    expect(advertised * 1_000_000).toBeLessThanOrEqual(MAX_MEDIA_BYTES);
  });
});

describe("P28: the composer never claims tracking the server would not do", () => {
  const composer = () => source("components/inbox/Composer.tsx");

  it("the promise sentence lives behind the toggle's own condition", () => {
    const src = composer();
    const promise = src.indexOf("swap the link for a trackable one");
    expect(promise).toBeGreaterThan(-1);
    // The nearest guard above the sentence must be the checkbox's state, not the mere
    // presence of a URL: the server rewrites links only when track_links is true.
    const before = src.slice(0, promise);
    expect(before.lastIndexOf("{trackClicks &&")).toBeGreaterThan(
      before.lastIndexOf("trackableUrlCount > 0 &&"),
    );
  });

  it("track_links is only ever sent as true alongside a real URL", () => {
    expect(composer()).toContain("if (trackClicks && trackableUrlCount > 0)");
  });
});

describe("P28: failure text in the timeline is the plain sentence", () => {
  const timeline = () => source("components/conversations/Timeline.tsx");

  it("renders failure_reason_public and never error_code", () => {
    const src = timeline();
    expect(src).toContain("item.failure_reason_public");
    // error_code stays on the wire for support, but no bubble may print it.
    expect(src).not.toContain("{item.error_code}");
  });
});

describe("P28: campaigns say that unhealthy numbers are skipped", () => {
  it("the From numbers hint no longer implies the whole pool always sends", () => {
    const src = source("pages/CampaignsPage.tsx");
    // JSX wraps the sentence across lines, so the whitespace has to be flexible.
    expect(src).toMatch(/delivery\s+trouble\s+is\s+skipped/);
  });
});

describe("P28: plain words in the new surfaces", () => {
  const SURFACES = [
    "components/inbox/Composer.tsx",
    "components/conversations/Timeline.tsx",
    "components/conversations/ScheduledDrawer.tsx",
    "api/messaging.ts",
  ];

  /** Same crude heuristic the P26 sweep uses: text nodes plus the attributes a screen
   * reader speaks. Candidates carrying code punctuation are discarded. */
  function visibleStrings(src: string): string[] {
    const out: string[] = [];
    for (const match of src.matchAll(/>([^<>{}]+)</g)) {
      const text = match[1].trim();
      if (!text || /[\n{}=;]/.test(text)) continue;
      out.push(text);
    }
    for (const match of src.matchAll(
      /(?:aria-label|title|placeholder|label)=\{?["`]([^"`]+)["`]/g,
    )) {
      out.push(match[1]);
    }
    // Customer sentences in api/messaging.ts are plain string literals, not JSX.
    for (const match of src.matchAll(/return "([A-Z][^"]{12,})"/g)) {
      out.push(match[1]);
    }
    return out;
  }

  it.each(SURFACES)("%s says none of MMS / carrier / DLR / E.164 / webhook", (rel) => {
    const hits = visibleStrings(source(rel)).filter((text) =>
      /\bMMS\b|\bDLR\b|\bcarrier\b|E\.164|webhook|\bSLA\b/i.test(text),
    );
    expect(hits).toEqual([]);
  });
});
