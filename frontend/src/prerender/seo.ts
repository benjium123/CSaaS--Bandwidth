import { FAQS } from "@/marketing/faq";
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
 *  page, SoftwareApplication on pricing, FAQPage on the FAQ, nothing elsewhere. */
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
  return [];
}

/** Canonical link, Open Graph/Twitter tags and JSON-LD, ready to drop before </head>. */
export function buildHead(path: string, meta: PageMeta): string {
  const url = canonicalUrl(path);
  const tags = [
    `<link rel="canonical" href="${escapeHtml(url)}" />`,
    `<meta property="og:type" content="website" />`,
    `<meta property="og:url" content="${escapeHtml(url)}" />`,
    `<meta property="og:title" content="${escapeHtml(meta.title)}" />`,
    `<meta property="og:description" content="${escapeHtml(meta.description)}" />`,
    `<meta name="twitter:card" content="summary" />`,
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
      `    <lastmod>${escapeHtml(lastmod)}</lastmod>`,
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
export function applyToTemplate(template: string, path: string, meta: PageMeta, bodyHtml: string): string {
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
    .replace("</head>", () => `${buildHead(path, meta)}\n  </head>`)
    .replace(rootAnchor, () => `<div id="root">${bodyHtml}</div>`);
}

/** The signed-in app shell: the built template plus a noindex robots meta. */
export function appShell(template: string): string {
  return template.replace("</head>", `  <meta name="robots" content="noindex" />\n  </head>`);
}
