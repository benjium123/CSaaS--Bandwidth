import * as React from "react";
import { Link } from "react-router-dom";
import { LogOut, ShieldCheck } from "lucide-react";
import { Button } from "@/components/ui/primitives";
import { useAuth } from "@/auth/AuthContext";
import { SETTINGS_ITEM, useRailNav } from "@/components/shell/Sidebar";

/**
 * App-root error boundary (bug-fix round item 22). A render error anywhere below this
 * (a bad response shape, a null-deref in a page component, etc.) previously blanked the
 * whole app to a white screen with no way back short of the browser's own reload button.
 * This catches it, shows a plain recovery screen, and offers a reload.
 *
 * P-: the fallback now also carries NAVIGATION, because "Reload" stopped being enough.
 * The icon Sidebar used to be the way out of a crashed page - it lives OUTSIDE this
 * boundary, so it survived whatever the page did. Then /inbox started hiding it (see
 * useHideIconRail in App.tsx: InboxColumn carries the full rail there, and two rails side
 * by side was the bug being fixed) - and InboxColumn renders INSIDE ConversationsPage,
 * i.e. inside this boundary. So a crash on the inbox took the only navigation with it and
 * left the user with a message, a Reload button and the browser's back button.
 *
 * The fix lives here rather than in Shell (i.e. "show the icon rail while the boundary is
 * errored") on purpose: this way it is not a patch for one route. EVERY route gets a way
 * out of a crash, including routes whose own chrome is inside the boundary, and the
 * boundary does not have to leak its state upward into Shell to get it.
 */

/**
 * The way out of a crashed page.
 *
 * Gating is NOT re-derived here - it calls the same `useRailNav` the icon rail and the
 * merged inbox rail call, so the recovery screen can never be the one surface that offers
 * a member a destination the rails would have hidden. Trust & safety follows Sidebar's own
 * `is_platform_operator` test for the same reason.
 *
 * Exported so App's Shell can pass it in; kept opt-in (see the `nav` prop) because the
 * boundary in main.tsx wraps the router and the auth provider themselves, and a fallback
 * that called these hooks unconditionally would throw while rendering the fallback - the
 * white screen this whole component exists to prevent.
 */
export function ErrorFallbackNav() {
  const { me, logout } = useAuth();
  const { items, canSeeSettings } = useRailNav();

  const destinations = [
    ...items,
    ...(me?.is_platform_operator
      ? [{ to: "/ops", label: "Trust & safety", icon: ShieldCheck }]
      : []),
    ...(canSeeSettings ? [SETTINGS_ITEM] : []),
  ];

  return (
    <nav aria-label="Error recovery" className="flex flex-wrap items-center justify-center gap-2">
      {destinations.map((item) => (
        <Link
          key={item.to}
          to={item.to}
          className="rounded-md border border-border px-3 py-1.5 text-sm text-foreground hover:bg-muted"
        >
          {item.label}
        </Link>
      ))}
      <Button type="button" variant="ghost" onClick={logout}>
        <LogOut className="mr-2 h-4 w-4" aria-hidden="true" />
        Sign out
      </Button>
    </nav>
  );
}

export class ErrorBoundary extends React.Component<
  {
    children: React.ReactNode;
    /** Rendered inside the fallback. Shell passes <ErrorFallbackNav />; the root boundary
     *  in main.tsx passes nothing, because no router or auth context exists above it. */
    nav?: React.ReactNode;
    /** Clearing the error on navigation is what makes `nav` actually work: React keeps a
     *  boundary in its error state forever otherwise, so a link click would change the URL
     *  and leave the user staring at the same "Something went wrong". Shell passes the
     *  current pathname. A boundary given no resetKey behaves exactly as it did before. */
    resetKey?: string;
  },
  { error: Error | null }
> {
  constructor(props: { children: React.ReactNode; nav?: React.ReactNode; resetKey?: string }) {
    super(props);
    this.state = { error: null };
  }

  static getDerivedStateFromError(error: Error): { error: Error } {
    return { error };
  }

  componentDidCatch(error: Error, info: React.ErrorInfo): void {
    // eslint-disable-next-line no-console
    console.error("Unhandled error in app tree", error, info.componentStack);
  }

  componentDidUpdate(prevProps: { resetKey?: string }): void {
    if (this.state.error && prevProps.resetKey !== this.props.resetKey) {
      this.setState({ error: null });
    }
  }

  render(): React.ReactNode {
    if (this.state.error) {
      return (
        <div className="flex h-full flex-col items-center justify-center gap-3 p-6 text-center">
          <h1 className="text-lg font-semibold">Something went wrong</h1>
          <p className="max-w-sm text-sm text-muted-foreground">
            {this.state.error.message || "An unexpected error occurred."}
          </p>
          {this.props.nav}
          <Button type="button" onClick={() => window.location.reload()}>
            Reload
          </Button>
        </div>
      );
    }
    return this.props.children;
  }
}
