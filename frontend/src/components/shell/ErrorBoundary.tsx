import * as React from "react";
import { Button } from "@/components/ui/primitives";

/**
 * App-root error boundary (bug-fix round item 22). A render error anywhere below this
 * (a bad response shape, a null-deref in a page component, etc.) previously blanked the
 * whole app to a white screen with no way back short of the browser's own reload button.
 * This catches it, shows a plain recovery screen, and offers a reload.
 */
export class ErrorBoundary extends React.Component<
  { children: React.ReactNode },
  { error: Error | null }
> {
  constructor(props: { children: React.ReactNode }) {
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

  render(): React.ReactNode {
    if (this.state.error) {
      return (
        <div className="flex h-full flex-col items-center justify-center gap-3 p-6 text-center">
          <h1 className="text-lg font-semibold">Something went wrong</h1>
          <p className="max-w-sm text-sm text-muted-foreground">
            {this.state.error.message || "An unexpected error occurred."}
          </p>
          <Button type="button" onClick={() => window.location.reload()}>
            Reload
          </Button>
        </div>
      );
    }
    return this.props.children;
  }
}
