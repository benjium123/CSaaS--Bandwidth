import { FAQS } from "@/marketing/faq";
import { OG_DIR, ogFileFor } from "./og";
import { postBySlug, type Post } from "@/marketing/blog/posts";
import { HOME_META, type PageMeta } from "@/marketing/pageMeta";
import { COVERAGE, PLANS, PRICED_PLANS, RATES, YEARLY, cents, money, type Plan } from "@/marketing/pricing.config";
import { SITE_URL, canonicalUrl, type PublicRoute } from "./routes";

/** Escape a string for use in HTML text or a double-quoted attribute value. */
export function escapeHtml(s: string): string {
  return s
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#39;");
}

function softwareApplication(): object {
  return {
    "@context": "https://schema.org",
    "@type": "SoftwareApplication",
    name: "Ringlite",
    applicationCategory: "BusinessApplication",
    operatingSystem: "Web, Android",
    url: `${SITE_URL}/`,
    offers: PRICED_PLANS.map(plan => ({
      "@type": "Offer",
      name: plan.name,
      price: String(plan.price),
      priceCurrency: "USD",
      description: plan.tagline,
      url: `${SITE_URL}/pricing`,
    })),
  };
}

/** Structured data for a page: Organization + WebSite + SoftwareApplication on the home
 *  page, SoftwareApplication on pricing, FAQPage on the FAQ, Blog/BlogPosting on the guides,
 *  nothing elsewhere. */
export function jsonLdFor(path: string): object[] {
  if (path === "/") {
    return [
      {
        "@context": "https://schema.org",
        "@type": "Organization",
        name: "Ringlite",
        url: `${SITE_URL}/`,
        logo: `${SITE_URL}/ringlite-mark.svg`,
      },
      {
        "@context": "https://schema.org",
        "@type": "WebSite",
        name: "Ringlite",
        url: `${SITE_URL}/`,
      },
      softwareApplication(),
    ];
  }
  if (path === "/pricing") return [softwareApplication()];
  if (path === "/faq") {
    return [
      {
        "@context": "https://schema.org",
        "@type": "FAQPage",
        mainEntity: FAQS.map(faq => ({
          "@type": "Question",
          name: faq.q,
          acceptedAnswer: { "@type": "Answer", text: faq.a },
        })),
      },
    ];
  }
  if (path === "/blog") {
    return [
      {
        "@context": "https://schema.org",
        "@type": "Blog",
        name: "Ringlite guides",
        url: canonicalUrl(path),
      },
    ];
  }
  if (path.startsWith("/blog/")) {
    const post = postBySlug(path.slice("/blog/".length));
    if (!post) return [];
    return [
      {
        "@context": "https://schema.org",
        "@type": "BlogPosting",
        headline: post.title,
        description: post.description,
        datePublished: post.published,
        dateModified: post.updated ?? post.published,
        author: { "@type": "Organization", name: "Ringlite" },
        publisher: {
          "@type": "Organization",
          name: "Ringlite",
          logo: { "@type": "ImageObject", url: `${SITE_URL}/ringlite-mark.svg` },
        },
        mainEntityOfPage: canonicalUrl(path),
      },
    ];
  }
  return [];
}

/** Canonical link, Open Graph/Twitter tags and JSON-LD, ready to drop before </head>. */
export function buildHead(path: string, meta: PageMeta, ogFile: string = ogFileFor(path)): string {
  const url = canonicalUrl(path);
  const image = `${SITE_URL}/${OG_DIR}/${ogFile}`;
  const ogType = path.startsWith("/blog/") ? "article" : "website";
  const tags = [
    `<link rel="canonical" href="${escapeHtml(url)}" />`,
    ...(path.startsWith("/blog")
      ? [`<link rel="alternate" type="application/rss+xml" title="Ringlite guides" href="${SITE_URL}/blog/rss.xml" />`]
      : []),
    `<meta property="og:type" content="${ogType}" />`,
    `<meta property="og:url" content="${escapeHtml(url)}" />`,
    `<meta property="og:title" content="${escapeHtml(meta.title)}" />`,
    `<meta property="og:description" content="${escapeHtml(meta.description)}" />`,
    `<meta property="og:image" content="${escapeHtml(image)}" />`,
    `<meta property="og:image:width" content="1200" />`,
    `<meta property="og:image:height" content="630" />`,
    `<meta property="og:image:alt" content="${escapeHtml(meta.title)}" />`,
    `<meta name="twitter:card" content="summary_large_image" />`,
    `<meta name="twitter:image" content="${escapeHtml(image)}" />`,
    `<meta name="twitter:title" content="${escapeHtml(meta.title)}" />`,
    `<meta name="twitter:description" content="${escapeHtml(meta.description)}" />`,
  ];
  for (const data of jsonLdFor(path)) {
    // Escape every "<" so the JSON can never close the surrounding <script> element.
    const json = JSON.stringify(data).replace(/</g, "\\u003c");
    tags.push(`<script type="application/ld+json">${json}</script>`);
  }
  return tags.join("\n");
}

export function buildSitemap(routes: PublicRoute[], lastmod: string): string {
  const entries = routes.map(route =>
    [
      "  <url>",
      `    <loc>${escapeHtml(canonicalUrl(route.path))}</loc>`,
      `    <lastmod>${escapeHtml(route.lastmod ?? lastmod)}</lastmod>`,
      `    <changefreq>${route.changefreq}</changefreq>`,
      `    <priority>${route.priority.toFixed(1)}</priority>`,
      "  </url>",
    ].join("\n"),
  );
  return [
    '<?xml version="1.0" encoding="UTF-8"?>',
    '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ...entries,
    "</urlset>",
    "",
  ].join("\n");
}

/** "2026-10-03" -> "Sat, 03 Oct 2026 00:00:00 GMT" (RFC 822). */
function rfc822(date: string): string {
  return new Date(`${date}T00:00:00Z`).toUTCString();
}

/** The guides feed, RSS 2.0. One <item> per post, newest first in the order given. */
export function buildRss(posts: Post[]): string {
  const items = posts.map(post => {
    const url = canonicalUrl(`/blog/${post.slug}`);
    return [
      "    <item>",
      `      <title>${escapeHtml(post.title)}</title>`,
      `      <link>${escapeHtml(url)}</link>`,
      `      <guid isPermaLink="true">${escapeHtml(url)}</guid>`,
      `      <pubDate>${rfc822(post.published)}</pubDate>`,
      `      <description>${escapeHtml(post.description)}</description>`,
      "    </item>",
    ].join("\n");
  });
  return [
    '<?xml version="1.0" encoding="UTF-8"?>',
    '<rss version="2.0">',
    "  <channel>",
    "    <title>Ringlite guides</title>",
    `    <link>${SITE_URL}/blog</link>`,
    "    <description>Plain guides on business phone costs, 10DLC texting registration and switching providers, from the Ringlite team.</description>",
    ...items,
    "  </channel>",
    "</rss>",
    "",
  ].join("\n");
}

function planLine(plan: Plan): string {
  const price = plan.price === null ? "custom quote" : `${money(plan.price)}/month`;
  const users = plan.included ? `${plan.included.users} ${plan.included.users === 1 ? "user" : "users"}` : "custom users";
  const numbers = plan.included ? `${plan.included.numbers} ${plan.included.numbers === 1 ? "number" : "numbers"}` : "custom numbers";
  const minutes = plan.minutes === null ? "custom call minutes" : plan.minutes === 0 ? "pay as you go" : `${plan.minutes.toLocaleString("en-US")} call minutes`;
  const limit = plan.maxUsers === null ? "no user limit" : `up to ${plan.maxUsers} users`;
  return `- ${plan.name}: ${price} \u2014 ${users}, ${numbers}, ${minutes}, ${limit}. ${plan.tagline}`;
}

/** llms.txt (llmstxt.org) built only from the pricing config and the page metadata. */
export function buildLlmsTxt(pages: { path: string; meta: PageMeta }[]): string {
  const lines: string[] = [
    "# Ringlite",
    `> ${HOME_META.description}`,
    "",
    `Ringlite is a business phone system: business numbers, calls and texts in one shared inbox, with an AI voice agent that answers calls. Each plan bundles users and phone numbers in one monthly price, and usage beyond the shared minute pool is billed at published rates. Calling and texting cover ${COVERAGE}. See plans and every rate at ${canonicalUrl("/pricing")}.`,
    "",
    "## Plans",
  ];
  for (const plan of PLANS) lines.push(planLine(plan));
  lines.push(`- Yearly billing: ${YEARLY.monthsBilled} months billed per year (${YEARLY.label}).`);
  lines.push("");
  lines.push("## Usage rates");
  lines.push(
    `- Calls past the minute pool: ${cents(RATES.minute)}/minute; texts ${cents(RATES.text)}; picture messages ${cents(RATES.picture)}; fax ${cents(RATES.faxPage)}/page. Coverage: ${COVERAGE}.`,
  );
  lines.push("");
  lines.push("## Pages");
  for (const page of pages) {
    lines.push(`- [${page.meta.title}](${canonicalUrl(page.path)}): ${page.meta.description}`);
  }
  lines.push("");
  return lines.join("\n");
}

/** Fill a built dist/index.html with one page's title, description, head and body. */
export function applyToTemplate(template: string, path: string, meta: PageMeta, bodyHtml: string, ogFile?: string): string {
  const titlePattern = /<title>[\s\S]*?<\/title>/;
  const descriptionPattern = /<meta\s+name="description"\s+content="[^"]*"\s*\/?>/;
  const rootAnchor = '<div id="root"></div>';
  if (!titlePattern.test(template)) throw new Error("index.html template is missing <title>");
  if (!descriptionPattern.test(template)) throw new Error("index.html template is missing the description meta tag");
  if (!template.includes(rootAnchor)) throw new Error('index.html template is missing <div id="root"></div>');

  // Function replacers: page text holds "$15" etc., and a string replacement reads "$'"/"$&" as patterns.
  return template
    .replace(titlePattern, () => `<title>${escapeHtml(meta.title)}</title>`)
    .replace(descriptionPattern, () => `<meta name="description" content="${escapeHtml(meta.description)}" />`)
    .replace("</head>", () => `${buildHead(path, meta, ogFile)}\n  </head>`)
    .replace(rootAnchor, () => `<div id="root">${bodyHtml}</div>`);
}

/** The signed-in app shell: the built template plus a noindex robots meta. */
export function appShell(template: string): string {
  return template.replace("</head>", `  <meta name="robots" content="noindex" />\n  </head>`);
}
