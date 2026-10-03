import { describe, expect, it } from "vitest";
import { COMPETITORS, PRODUCTS, SOLUTIONS } from "@/marketing/content";
import { FAQS } from "@/marketing/faq";
import { HOME_META, type PageMeta } from "@/marketing/pageMeta";
import { PLANS } from "@/marketing/pricing.config";
import { isMarketingPath } from "@/marketing/routes";
import { render } from "./entry-server";
import { PUBLIC_ROUTES, canonicalUrl } from "./routes";
import { applyToTemplate, appShell, buildHead, buildLlmsTxt, buildSitemap } from "./seo";

interface JsonLd {
  "@type": string;
  mainEntity?: unknown[];
}

function jsonLdBlocks(head: string): string[] {
  return Array.from(head.matchAll(/<script type="application\/ld\+json">([\s\S]*?)<\/script>/g)).map(match => match[1] ?? "");
}

describe("PUBLIC_ROUTES", () => {
  it("has no duplicate paths", () => {
    const paths = PUBLIC_ROUTES.map(route => route.path);
    expect(new Set(paths).size).toBe(paths.length);
  });

  it("lists every product, solution and competitor slug", () => {
    const paths = PUBLIC_ROUTES.map(route => route.path);
    for (const product of PRODUCTS) expect(paths).toContain(`/product/${product.slug}`);
    for (const solution of SOLUTIONS) expect(paths).toContain(`/solutions/${solution.slug}`);
    for (const competitor of COMPETITORS) expect(paths).toContain(`/compare/${competitor.slug}`);
  });

  it("routes every non-root path through the marketing matcher", () => {
    for (const route of PUBLIC_ROUTES) {
      if (route.path === "/") continue;
      expect(isMarketingPath(route.path), route.path).toBe(true);
    }
  });

  it("canonicalises the root and a normal path", () => {
    expect(canonicalUrl("/")).toBe("https://ringlite.io/");
    expect(canonicalUrl("/pricing")).toBe("https://ringlite.io/pricing");
  });
});

describe("render", () => {
  it("renders every public page with an <h1>, a meta title and no duplicate titles", () => {
    const titles = new Set<string>();
    for (const route of PUBLIC_ROUTES) {
      const { html, meta } = render(route.path);
      expect(html, route.path).toContain("<h1");
      expect(meta, route.path).not.toBeNull();
      const title = meta?.title ?? "";
      expect(titles.has(title), `duplicate title "${title}"`).toBe(false);
      titles.add(title);
    }
  });

  it("uses HOME_META for the homepage", () => {
    expect(render("/").meta?.title).toBe(HOME_META.title);
  });
});

describe("applyToTemplate", () => {
  const template = [
    "<!doctype html><html><head>",
    "<title>Old title</title>",
    '<meta name="description" content="old description" />',
    "</head><body>",
    '<div id="root"></div>',
    "</body></html>",
  ].join("\n");

  it("injects the body, replaces the title and adds the head", () => {
    const meta: PageMeta = { title: 'Pricing "page"', description: 'Plans from $15 & <notes> "here"' };
    const out = applyToTemplate(template, "/pricing", meta, "<h1>Pricing</h1>");
    expect(out).toContain("<title>Pricing &quot;page&quot;</title>");
    expect(out).toContain('<div id="root"><h1>Pricing</h1></div>');
    expect(out).toContain('<link rel="canonical" href="https://ringlite.io/pricing" />');
    expect(out).toContain('content="Plans from $15 &amp; &lt;notes&gt; &quot;here&quot;"');
  });

  it("throws when the root anchor is missing", () => {
    const broken = '<!doctype html><html><head><title>x</title><meta name="description" content="y" /></head><body></body></html>';
    expect(() => applyToTemplate(broken, "/", HOME_META, "")).toThrow();
  });
});

describe("buildHead", () => {
  it("emits the canonical link and parseable FAQ JSON-LD", () => {
    const head = buildHead("/faq", { title: "FAQ", description: "Answers" });
    expect(head).toContain('<link rel="canonical" href="https://ringlite.io/faq" />');
    expect(head).toContain('<meta property="og:type" content="website" />');

    const blocks = jsonLdBlocks(head);
    expect(blocks.length).toBeGreaterThan(0);
    const faq = blocks.map(block => JSON.parse(block) as JsonLd).find(obj => obj["@type"] === "FAQPage");
    expect(faq).toBeDefined();
    expect(faq?.mainEntity).toHaveLength(FAQS.length);
    for (const block of blocks) expect(block).not.toContain("</script");
  });
});

describe("buildSitemap", () => {
  it("lists one <loc> per route inside a urlset", () => {
    const sitemap = buildSitemap(PUBLIC_ROUTES, "2026-01-01");
    expect(sitemap).toContain('<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">');
    expect((sitemap.match(/<loc>/g) ?? []).length).toBe(PUBLIC_ROUTES.length);
  });
});

describe("appShell", () => {
  it("adds exactly one robots noindex meta", () => {
    const shell = appShell('<!doctype html><html><head><title>x</title></head><body><div id="root"></div></body></html>');
    expect((shell.match(/name="robots"/g) ?? []).length).toBe(1);
    expect(shell).toContain('<meta name="robots" content="noindex" />');
  });
});

describe("buildLlmsTxt", () => {
  it("starts with the site heading and lists the plans and the pricing URL", () => {
    const txt = buildLlmsTxt([{ path: "/", meta: HOME_META }]);
    expect(txt.startsWith("# Ringlite")).toBe(true);
    for (const plan of PLANS) expect(txt).toContain(plan.name);
    expect(txt).toContain("https://ringlite.io/pricing");
  });
});
