import { mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { pathToFileURL } from "node:url";
import { JSDOM } from "jsdom";
import { build } from "vite";

// The app reads localStorage (AuthContext) during render, so stand up a DOM before the app
// code is imported. navigator is a read-only getter on Node 22, hence defineProperty.
const dom = new JSDOM("<!doctype html><html><head></head><body></body></html>", {
  url: "https://ringlite.io/",
});

function setGlobal(name, value) {
  Object.defineProperty(globalThis, name, { value, configurable: true, writable: true });
}

setGlobal("window", dom.window);
setGlobal("document", dom.window.document);
setGlobal("localStorage", dom.window.localStorage);
setGlobal("sessionStorage", dom.window.sessionStorage);
setGlobal("location", dom.window.location);
setGlobal("HTMLElement", dom.window.HTMLElement);
setGlobal("Node", dom.window.Node);
setGlobal("getComputedStyle", dom.window.getComputedStyle.bind(dom.window));
setGlobal("matchMedia", () => ({
  matches: false,
  addEventListener() {},
  removeEventListener() {},
  addListener() {},
  removeListener() {},
}));
Object.defineProperty(globalThis, "navigator", { value: dom.window.navigator, configurable: true });
// Components that reach for window.matchMedia directly should get the same stub.
Object.defineProperty(dom.window, "matchMedia", { value: globalThis.matchMedia, configurable: true, writable: true });

// Build the SSR entry through Vite's JS API so vite.config.ts (the "@" alias) is applied.
await build({
  logLevel: "warn",
  build: {
    ssr: "src/prerender/entry-server.tsx",
    outDir: "dist-ssr",
    emptyOutDir: true,
    copyPublicDir: false,
  },
  ssr: { noExternal: [/^@fontsource/, /\.css$/] },
});

const ssr = await import(pathToFileURL(resolve("dist-ssr/entry-server.js")).href);

// AuthProvider's useLayoutEffect warns once per page on the server. The browser re-renders
// with createRoot (no hydration), so the warning has nothing to protect here.
const consoleError = console.error;
console.error = (...args) => {
  if (typeof args[0] === "string" && args[0].includes("useLayoutEffect does nothing on the server")) return;
  consoleError(...args);
};

// dist/index.html is both the template and the "/" output: read it before writing anything.
const template = readFileSync("dist/index.html", "utf8");
writeFileSync("dist/app.html", ssr.appShell(template));

const pages = [];
const titles = new Set();

for (const route of ssr.PUBLIC_ROUTES) {
  let result;
  try {
    result = ssr.render(route.path);
  } catch (err) {
    console.error(`prerender: ${route.path} failed to render: ${err && err.message ? err.message : err}`);
    process.exit(1);
  }

  const { html, meta } = result;
  if (!meta) {
    console.error(`prerender: ${route.path} reported no page meta`);
    process.exit(1);
  }

  const text = html
    .replace(/<[^>]*>/g, " ")
    .replace(/\s+/g, " ")
    .trim();
  if (text.length < 600) {
    console.error(`prerender: ${route.path} produced only ${text.length} characters of visible text`);
    process.exit(1);
  }
  if (!html.includes("<h1")) {
    console.error(`prerender: ${route.path} produced no <h1>`);
    process.exit(1);
  }
  if (titles.has(meta.title)) {
    console.error(`prerender: duplicate title "${meta.title}" for ${route.path}`);
    process.exit(1);
  }
  titles.add(meta.title);

  const outPath = route.path === "/" ? "dist/index.html" : `dist${route.path}/index.html`;
  mkdirSync(dirname(outPath), { recursive: true });
  const page = ssr.applyToTemplate(template, route.path, meta, html);
  writeFileSync(outPath, page);
  pages.push({ path: route.path, meta });

  const kb = (Buffer.byteLength(page) / 1024).toFixed(1);
  console.log(`${route.path}  ${meta.title}  ${kb} KB`);
}

const lastmod = new Date().toISOString().slice(0, 10);
writeFileSync("dist/sitemap.xml", ssr.buildSitemap(ssr.PUBLIC_ROUTES, lastmod));
writeFileSync("dist/llms.txt", ssr.buildLlmsTxt(pages));

rmSync("dist-ssr", { recursive: true, force: true });
console.log(`prerendered ${pages.length} pages`);
