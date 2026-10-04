/**
 * Social preview images (1200x630 PNG) for every public page, written to public/og/.
 * Run by hand after prices, competitors or guides change:
 *   npx vite-node scripts/og-images.ts
 * Renders each card with the locally installed Chrome (headless), so the build never depends on
 * a browser. Every number comes from pricing.config, every title from the content sources.
 */
import { execFileSync } from "node:child_process";
import { existsSync, mkdirSync, mkdtempSync, rmSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { COMPETITORS } from "@/marketing/content";
import { POSTS } from "@/marketing/blog/posts";
import { COMPETITOR_SEAT_PRICES, competitorCost, competitorTier, money, recommend } from "@/marketing/pricing.config";
import { OG_DIR, ogFileFor } from "@/prerender/og";

const CHROME = [
  process.env.CHROME_PATH,
  "C:/Program Files/Google/Chrome/Application/chrome.exe",
  "C:/Program Files (x86)/Google/Chrome/Application/chrome.exe",
  "/usr/bin/google-chrome",
  "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
].find((p): p is string => !!p && existsSync(p));
if (!CHROME) throw new Error("Chrome not found; set CHROME_PATH");

const fonts = resolve("node_modules/@fontsource-variable");
const ARCHIVO = pathToFileURL(join(fonts, "archivo/files/archivo-latin-wght-normal.woff2")).href;
const ONEST = pathToFileURL(join(fonts, "onest/files/onest-latin-wght-normal.woff2")).href;
const TEAM = 10;

interface Card {
  file: string;
  eyebrow: string;
  /** Title HTML; wrap the accent part in <em>. */
  title: string;
  sub: string;
  /** Right-hand panel: cost bars, a head-to-head, or nothing. */
  panel?: { kind: "bars"; slugs?: string[] } | { kind: "versus"; slug: string };
}

const esc = (s: string) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
const shortName = (name: string) => name.replace(/\s*\([^)]*\)\s*$/, "");

function costRows(slugs?: string[]) {
  const ours = recommend(TEAM, TEAM);
  const rows = [
    { label: `Ringlite ${ours.plan.name}`, value: ours.monthly ?? 0, us: true },
    ...COMPETITOR_SEAT_PRICES.filter(c => !slugs || slugs.includes(c.slug)).map(c => ({
      label: competitorTier(c, TEAM).name, value: competitorCost(c, TEAM, TEAM), us: false,
    })),
  ].sort((a, b) => a.value - b.value);
  return rows;
}

function panelHtml(card: Card): string {
  if (!card.panel) return "";
  if (card.panel.kind === "bars") {
    const rows = costRows(card.panel.slugs).slice(0, 6);
    const max = Math.max(...rows.map(r => r.value));
    return `<div class="panel"><p class="cap">${TEAM} PEOPLE · ${TEAM} NUMBERS · PER MONTH</p>${rows.map(r =>
      `<div class="row${r.us ? " us" : ""}"><span class="lbl">${esc(r.label)}</span><span class="track"><i style="width:${(r.value / max) * 100}%"></i></span><b>${money(Math.round(r.value))}</b></div>`).join("")}</div>`;
  }
  const c = COMPETITOR_SEAT_PRICES.find(p => p.slug === card.panel!.slug);
  if (!c) return "";
  const ours = recommend(TEAM, TEAM);
  const theirs = Math.round(competitorCost(c, TEAM, TEAM));
  return `<div class="panel versus"><p class="cap">${TEAM} PEOPLE · ${TEAM} NUMBERS · PER MONTH</p>
    <div class="vs"><div class="us"><span>Ringlite ${esc(ours.plan.name)}</span><strong>${money(ours.monthly ?? 0)}</strong></div>
    <div><span>${esc(competitorTier(c, TEAM).name)}</span><strong>${money(theirs)}</strong></div></div></div>`;
}

function html(card: Card): string {
  return `<!doctype html><html><head><meta charset="utf-8"><style>
@font-face{font-family:Archivo;src:url(${ARCHIVO}) format("woff2");font-weight:100 900}
@font-face{font-family:Onest;src:url(${ONEST}) format("woff2");font-weight:100 900}
*{box-sizing:border-box;margin:0}
html,body{width:1200px;height:630px;overflow:hidden}
body{background:#0D1320;color:#EEF2FA;font-family:Onest,sans-serif;position:relative}
body::before{content:"";position:absolute;inset:0;background:radial-gradient(900px 520px at 88% -10%,rgba(59,118,232,.38),transparent 62%),radial-gradient(600px 400px at -5% 110%,rgba(59,118,232,.16),transparent 60%)}
body::after{content:"";position:absolute;inset:0;background-image:radial-gradient(rgba(238,242,250,.07) 1px,transparent 1px);background-size:18px 18px;mask-image:linear-gradient(90deg,transparent 40%,#000)}
.wrap{position:relative;z-index:1;height:100%;padding:56px 64px;display:grid;grid-template-columns:${card.panel ? "1.08fr .92fr" : "1fr"};gap:48px;align-items:center}
.brand{position:absolute;top:52px;left:64px;display:flex;align-items:center;gap:12px;font:780 34px Archivo;letter-spacing:-1.5px;z-index:2}
.mark{display:inline-flex;gap:4px;align-items:center;height:30px}.mark i{display:block;width:6px;border-radius:6px;background:#3B76E8;transform:rotate(24deg)}
.mark i:nth-child(1){height:15px}.mark i:nth-child(2){height:30px}.mark i:nth-child(3){height:22px}
.copy{padding-top:70px}
.eyebrow{font:600 15px Archivo;letter-spacing:.14em;color:#8FB3F5;text-transform:uppercase}
h1{font:850 ${card.title.length > 70 ? 50 : card.title.length > 44 ? 60 : 76}px/1.0 Archivo;letter-spacing:-.045em;margin-top:18px;text-wrap:balance}
h1 em{font-style:normal;color:#5B8FF0}
.sub{font-size:23px;line-height:1.45;color:#B7C2D6;margin-top:22px;max-width:30ch}
.url{position:absolute;bottom:44px;left:64px;font:600 17px Archivo;letter-spacing:.06em;color:#8FB3F5;z-index:2}
.panel{background:rgba(255,255,255,.05);border:1px solid rgba(238,242,250,.14);border-radius:22px;padding:28px 28px 24px;display:grid;gap:14px;margin-top:60px}
.cap{font:600 13px Archivo;letter-spacing:.12em;color:#93A0B8}
.row{display:grid;grid-template-columns:200px 1fr 66px;gap:12px;align-items:center;font-size:17px;color:#B7C2D6}
.row .lbl{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.track{height:12px;border-radius:6px;background:rgba(238,242,250,.1);overflow:hidden}.track i{display:block;height:100%;border-radius:6px;background:rgba(238,242,250,.38)}
.row b{font:700 18px Archivo;text-align:right}
.row.us{color:#fff;font-weight:600}.row.us .track i{background:#3B76E8}.row.us b{color:#6E9CF2;font-size:21px}
.vs{display:grid;gap:16px}.vs>div{display:flex;justify-content:space-between;align-items:baseline;padding:18px 20px;border-radius:14px;background:rgba(238,242,250,.06)}
.vs span{font-size:20px;color:#B7C2D6}.vs strong{font:800 44px Archivo;letter-spacing:-.03em}
.vs .us{background:rgba(59,118,232,.22);border:1px solid rgba(110,156,242,.5)}.vs .us span{color:#fff}.vs .us strong{color:#8FB3F5}
</style></head><body><div class="brand"><span class="mark"><i></i><i></i><i></i></span>ringlite</div>
<div class="wrap"><div class="copy"><p class="eyebrow">${esc(card.eyebrow)}</p><h1>${card.title}</h1><p class="sub">${esc(card.sub)}</p></div>${panelHtml(card)}</div>
<div class="url">RINGLITE.IO</div></body></html>`;
}

const cards: Card[] = [
  { file: ogFileFor("/"), eyebrow: "AI-ready business phone for every team", title: "Your team. Your numbers. <em>One price.</em>", sub: "Calls, texts and voicemail in one shared inbox. No fee per seat.", panel: { kind: "bars" } },
  { file: ogFileFor("/pricing"), eyebrow: "Pricing", title: "One price for your team <em>and your numbers.</em>", sub: "Starter $15, Team $45, Business $130 a month. Every rate published.", panel: { kind: "bars" } },
  { file: ogFileFor("/calculator"), eyebrow: "Cost calculator", title: "What does a phone system cost <em>for your team?</em>", sub: "Price your team on Ringlite and on per-seat tools, side by side.", panel: { kind: "bars" } },
  { file: ogFileFor("/blog"), eyebrow: "Guides", title: "Guides for teams <em>on the phone.</em>", sub: "Phone costs, 10DLC texting registration and switching providers." },
  { file: ogFileFor("/__default"), eyebrow: "AI-ready business phone for every team", title: "Calls, texts and voicemail. <em>One shared inbox.</em>", sub: "Local numbers, registered texting and your whole team on one price." },
  ...COMPETITORS.filter(c => COMPETITOR_SEAT_PRICES.some(p => p.slug === c.slug)).map(c => ({
    file: ogFileFor(`/compare/${c.slug}`), eyebrow: `${shortName(c.name)} alternative`,
    title: `Ringlite vs <em>${esc(shortName(c.name))}</em>`, sub: c.summary.split(". ")[1] ?? c.summary,
    panel: { kind: "versus" as const, slug: c.slug },
  })),
  ...POSTS.filter(p => !p.draft).map(p => ({
    file: ogFileFor(`/blog/${p.slug}`), eyebrow: `Guide · ${p.topic}`, title: esc(p.title), sub: `${p.readMinutes} min read · ringlite.io/blog`,
  })),
];

const outDir = resolve("public", OG_DIR);
mkdirSync(outDir, { recursive: true });
const tmp = mkdtempSync(join(tmpdir(), "og-"));
try {
  for (const card of cards) {
    const page = join(tmp, card.file.replace(/\.png$/, ".html"));
    writeFileSync(page, html(card));
    execFileSync(CHROME, [
      "--headless=new", "--disable-gpu", "--hide-scrollbars", "--force-device-scale-factor=1",
      `--user-data-dir=${join(tmp, "profile")}`, "--window-size=1200,630",
      `--screenshot=${join(outDir, card.file)}`, pathToFileURL(page).href,
    ], { stdio: "ignore" });
    console.log(card.file);
  }
} finally {
  rmSync(tmp, { recursive: true, force: true });
}
console.log(`${cards.length} images in public/${OG_DIR}`);
