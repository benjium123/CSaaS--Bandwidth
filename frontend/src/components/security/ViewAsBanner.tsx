/**
 * H3 support view banner: a fixed bar shown while the operator is looking at a customer
 * workspace through a ViewAs grant. It counts down the grant and offers the way back.
 *
 * The grant lives in sessionStorage, so this reads it afresh every second: another tab
 * (or the server) can end the view, and once the grant runs out we end it locally (best
 * effort) and drop the operator back on their own workspace.
 */
import * as React from "react";

import { useAuth } from "@/auth/AuthContext";
import { clearViewAs, loadViewAs, type ViewAs } from "@/auth/viewAs";
import { Button } from "@/components/ui/primitives";

/** Remaining time as mm:ss, floored and clamped at zero (90_000 -> "01:30"). */
export function formatRemaining(ms: number): string {
  const totalSeconds = Math.max(0, Math.floor(ms / 1000));
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${String(minutes).padStart(2, "0")}:${String(seconds).padStart(2, "0")}`;
}

export function ViewAsBanner() {
  const { api } = useAuth();
  const [view, setViewState] = React.useState<ViewAs | null>(() => loadViewAs());
  const [now, setNow] = React.useState(() => Date.now());

  const viewRef = React.useRef<ViewAs | null>(view);
  const exitingRef = React.useRef(false);

  const setView = React.useCallback((next: ViewAs | null) => {
    viewRef.current = next;
    setViewState(next);
  }, []);

  const exit = React.useCallback(
    async (current: ViewAs) => {
      if (exitingRef.current) return;
      exitingRef.current = true;
      try {
        await api.request(`/api/v1/ops/view-as/${current.id}/end`, { method: "POST" });
      } catch {
        // The server is the real gate; a failed end must not trap the operator here.
      }
      clearViewAs();
      api.setAuth({ orgId: current.prevOrgId });
      window.location.assign("/ops");
    },
    [api],
  );

  React.useEffect(() => {
    function tick() {
      setNow(Date.now());
      const next = loadViewAs();
      if (next) {
        setView(next);
        return;
      }
      const current = viewRef.current;
      if (current) {
        // The grant just expired or was cleared between ticks: leave the workspace.
        void exit(current);
      } else {
        setView(null);
      }
    }

    const interval = window.setInterval(tick, 1000);
    return () => window.clearInterval(interval);
  }, [exit, setView]);

  if (!view) return null;

  const remaining = Date.parse(view.expiresAt) - now;

  return (
    <div
      role="status"
      className="fixed inset-x-0 top-0 z-50 flex flex-wrap items-center justify-center gap-3 bg-destructive px-4 py-2 text-sm text-destructive-foreground"
    >
      <span>
        Support view of <strong>{view.orgName}</strong> · read-only · ends in{" "}
        {formatRemaining(remaining)}
      </span>
      <Button
        type="button"
        variant="outline"
        size="sm"
        onClick={() => {
          void exit(view);
        }}
      >
        Exit view
      </Button>
    </div>
  );
}
