import { existsSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { OG_DEFAULT, OG_DIR, ogFileFor } from "./og";
import { PUBLIC_ROUTES } from "./routes";
import { buildHead } from "./seo";

describe("social preview images", () => {
  it("has a rendered card for every public page (run scripts/og-images.ts after adding pages)", () => {
    const missing = PUBLIC_ROUTES.map(r => ogFileFor(r.path)).filter(f => !existsSync(resolve("public", OG_DIR, f)));
    expect(missing).toEqual([]);
    expect(existsSync(resolve("public", OG_DIR, OG_DEFAULT))).toBe(true);
  });

  it("puts a large image card in every page head", () => {
    const head = buildHead("/compare/quo", { title: "T", description: "D" });
    expect(head).toContain('<meta property="og:image" content="https://ringlite.io/og/compare-quo.png" />');
    expect(head).toContain('<meta name="twitter:card" content="summary_large_image" />');
  });
});
