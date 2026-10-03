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
  title: "Ringlite — Business Phone System with AI Agents, Calls & Texts",
  description:
    "Business numbers, calls and texts in one shared inbox, with an AI voice agent that answers calls. Plans from $15/month; Team includes 3 users, 3 numbers and 200 call minutes.",
};
