/**
 * H3 view as workspace: a platform operator's read-only, time-boxed look at a customer
 * workspace. The grant lives in sessionStorage (this tab only, gone when the tab closes);
 * api/client.ts sends it as X-View-As with every request and AuthContext adds a synthetic
 * read-only membership for the workspace, so the normal app renders it. The server is the
 * real gate: it refuses every write and every request once the grant has ended.
 */
export type ViewAs = {
  id: string;
  orgId: string;
  orgName: string;
  expiresAt: string;
  permissions: string[];
  /** The operator's own workspace before the view, restored on exit. */
  prevOrgId: string | null;
};

const KEY = "csaas.viewAs";
export const VIEW_AS_HEADER = "X-View-As";

/** The live view, or null. ``includeExpired`` also returns one past its expiry (for cleanup). */
export function loadViewAs({ includeExpired = false } = {}): ViewAs | null {
  try {
    const raw = sessionStorage.getItem(KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as ViewAs;
    if (!parsed?.id || !parsed.orgId) return null;
    if (!includeExpired && Date.parse(parsed.expiresAt) <= Date.now()) return null;
    return parsed;
  } catch {
    return null;
  }
}

export function saveViewAs(view: ViewAs): void {
  try {
    sessionStorage.setItem(KEY, JSON.stringify(view));
  } catch {
    /* storage blocked - the view simply will not start */
  }
}

export function clearViewAs(): void {
  try {
    sessionStorage.removeItem(KEY);
  } catch {
    /* nothing stored */
  }
}
