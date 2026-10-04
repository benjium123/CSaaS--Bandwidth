import * as React from "react";

export interface PageMeta {
  title: string;
  description: string;
}

/**
 * Set only by the build-time prerender (src/prerender/entry-server.tsx), which renders each
 * public page to static HTML so crawlers that never run JavaScript still read it. Effects do
 * not run there, so pages report their title/description during render instead. In the
 * browser no provider exists and this is a no-op.
 */
export const PageMetaContext = React.createContext<((meta: PageMeta) => void) | null>(null);

export function useReportPageMeta(meta: PageMeta): void {
  React.useContext(PageMetaContext)?.(meta);
}

/** The homepage's title and description; index.html carries the same pair for the bare shell. */
export const HOME_META: PageMeta = {
  title: "AI-Ready Business Phone System for Every Team | Ringlite",
  description:
    "Calls, texts and voicemail in one shared inbox, with one price for your team and numbers. Team is $45/month for 3 users, 3 numbers and 200 call minutes. No fee per seat.",
};
