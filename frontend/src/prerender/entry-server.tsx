import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { renderToString } from "react-dom/server";
import { StaticRouter } from "react-router-dom/server";
import { AuthProvider } from "@/auth/AuthContext";
import { PageMetaContext, type PageMeta } from "@/marketing/pageMeta";
import { MarketingRoutes } from "@/marketing/routes";
import { LandingPage } from "@/pages/LandingPage";

/**
 * Render one public page to static HTML at build time. Pages report their own title and
description during render through PageMetaContext; effects never run here, so the provider
 * captures the reports. The LAST report wins, and a fresh QueryClient per call keeps renders
 * independent.
 */
export function render(path: string): { html: string; meta: PageMeta | null } {
  const captured: { meta: PageMeta | null } = { meta: null };
  const html = renderToString(
    <QueryClientProvider client={new QueryClient()}>
      <AuthProvider>
        <PageMetaContext.Provider
          value={meta => {
            captured.meta = meta;
          }}
        >
          <StaticRouter location={path}>{path === "/" ? <LandingPage /> : <MarketingRoutes />}</StaticRouter>
        </PageMetaContext.Provider>
      </AuthProvider>
    </QueryClientProvider>,
  );
  return { html, meta: captured.meta };
}

export { PUBLIC_ROUTES, canonicalUrl } from "./routes";
export { OG_DEFAULT, OG_DIR, ogFileFor } from "./og";
export { applyToTemplate, appShell, buildSitemap, buildLlmsTxt, buildRss } from "./seo";
export { PUBLISHED_POSTS } from "@/marketing/blog/posts";
