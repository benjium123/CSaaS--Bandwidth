import { ALTERNATIVE_SLUGS, COMPETITORS, PRODUCTS, SOLUTIONS } from "@/marketing/content";

/** One page the build-time prerender writes to static HTML and lists in sitemap.xml. */
export interface PublicRoute {
  path: string;
  priority: number;
  changefreq: "weekly" | "monthly" | "yearly";
}

export const SITE_URL = "https://ringlite.io";

/**
 * Every public page, in sitemap order. Slugs come from the marketing content so a new
 * product, solution, competitor or alternative page appears here (and in the sitemap)
 * automatically. The /privacy, /terms and /refunds aliases are left out: nginx 301s them to
 * /legal/*.
 */
export const PUBLIC_ROUTES: PublicRoute[] = [
  { path: "/", priority: 1.0, changefreq: "weekly" },
  { path: "/pricing", priority: 0.9, changefreq: "weekly" },
  { path: "/calculator", priority: 0.8, changefreq: "monthly" },
  ...PRODUCTS.map(product => ({ path: `/product/${product.slug}`, priority: 0.8, changefreq: "monthly" as const })),
  ...SOLUTIONS.map(solution => ({ path: `/solutions/${solution.slug}`, priority: 0.7, changefreq: "monthly" as const })),
  ...COMPETITORS.map(competitor => ({ path: `/compare/${competitor.slug}`, priority: 0.7, changefreq: "monthly" as const })),
  ...ALTERNATIVE_SLUGS.map(slug => ({ path: `/alternatives/${slug}`, priority: 0.8, changefreq: "monthly" as const })),
  { path: "/switch", priority: 0.7, changefreq: "monthly" },
  { path: "/faq", priority: 0.6, changefreq: "monthly" },
  { path: "/sales", priority: 0.5, changefreq: "monthly" },
  { path: "/trust", priority: 0.5, changefreq: "monthly" },
  { path: "/legal/911", priority: 0.3, changefreq: "yearly" },
  { path: "/legal/privacy", priority: 0.3, changefreq: "yearly" },
  { path: "/legal/terms", priority: 0.3, changefreq: "yearly" },
  { path: "/legal/refunds", priority: 0.3, changefreq: "yearly" },
];

/** "/" -> "https://ringlite.io/", "/pricing" -> "https://ringlite.io/pricing". */
export function canonicalUrl(path: string): string {
  return `${SITE_URL}${path}`;
}
