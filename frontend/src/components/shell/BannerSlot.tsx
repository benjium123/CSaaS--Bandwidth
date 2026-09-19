/**
 * ONE console banner at a time.
 *
 * The four shell banners (passkey grace, business verification, safety monitoring, low
 * balance) each decide their own visibility from their own data, and several can be true
 * at once — a brand-new workspace with no credits and an unsubmitted KYC application hits
 * three of them. Stacked as tall cards they ate half the inbox viewport, which is the bug
 * this file exists to fix.
 *
 * The arbitration is deliberately NOT "recompute every banner's condition in one place":
 * that would be each banner's rule written twice, and the copy in this file would drift
 * from the copy in theirs. Instead each banner keeps its own guard and wraps whatever it
 * decided to render in a <BannerSlot priority={...}>. A slot CLAIMS its priority on mount
 * (layout effect, so the arbitration settles before paint) and renders its children only
 * while no lower number is claimed. A banner that returned null never mounts a slot and so
 * never claims — the winner is always a banner that genuinely wanted to be seen.
 *
 * Outside a <BannerRegion> a slot is transparent: it renders its children unconditionally.
 * That keeps every existing standalone banner test (which mounts one banner with no shell
 * around it) meaningful rather than accidentally passing because of this machinery.
 */
import * as React from "react";

/**
 * Lower number wins. The order is "what is broken right now" before "what will break".
 *
 * 1. monitoring - the platform itself has restricted or paused this workspace's traffic.
 *    It is the only banner that reports an action taken AGAINST the account, the only one
 *    carrying an appeal, and nothing the workspace does elsewhere in the console changes it.
 * 2. credits - a hard stop on sending that the workspace can clear itself, immediately.
 * 3. verification - a gate on texting, but one that is also the "Register for texting" row
 *    of the setup checklist, which now has its own page and its own rail entry. It has a
 *    second discovery path; the two above it have none.
 * 4. passkey grace - a deadline. Real, but nothing is broken yet, and after the deadline it
 *    only blocks admin features, not the day's work.
 */
export const BANNER_PRIORITY = {
  monitoring: 10,
  credits: 20,
  verification: 30,
  passkey: 40,
} as const;

type Actions = {
  claim: (priority: number) => void;
  release: (priority: number) => void;
};

const ActionsContext = React.createContext<Actions | null>(null);
/** The lowest currently-claimed priority, or null when nothing is claimed. */
const TopContext = React.createContext<number | null>(null);

export function BannerRegion({ children }: { children: React.ReactNode }) {
  // Counts rather than a set: React may mount a replacement slot before unmounting the old
  // one, and StrictMode runs effects twice. A count cannot be driven negative by that.
  const countsRef = React.useRef(new Map<number, number>());
  const [top, setTop] = React.useState<number | null>(null);

  const recompute = React.useCallback(() => {
    let lowest: number | null = null;
    for (const [priority, count] of countsRef.current) {
      if (count > 0 && (lowest === null || priority < lowest)) lowest = priority;
    }
    setTop(lowest);
  }, []);

  const actions = React.useMemo<Actions>(
    () => ({
      claim: (priority) => {
        countsRef.current.set(priority, (countsRef.current.get(priority) ?? 0) + 1);
        recompute();
      },
      release: (priority) => {
        countsRef.current.set(priority, Math.max(0, (countsRef.current.get(priority) ?? 0) - 1));
        recompute();
      },
    }),
    [recompute],
  );

  return (
    <ActionsContext.Provider value={actions}>
      <TopContext.Provider value={top}>{children}</TopContext.Provider>
    </ActionsContext.Provider>
  );
}

export function BannerSlot({
  priority,
  children,
}: {
  priority: number;
  children: React.ReactNode;
}) {
  const actions = React.useContext(ActionsContext);
  const top = React.useContext(TopContext);

  // `actions` is stable for the life of the region, so this claim runs once per mount and
  // is not re-run when another banner wins.
  React.useLayoutEffect(() => {
    if (!actions) return;
    actions.claim(priority);
    return () => actions.release(priority);
  }, [actions, priority]);

  if (!actions) return <>{children}</>;
  if (top !== null && top < priority) return null;
  return <>{children}</>;
}
