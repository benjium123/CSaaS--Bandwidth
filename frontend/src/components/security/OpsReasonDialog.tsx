import * as React from "react";

import { useAuth } from "@/auth/AuthContext";
import { surfaceThemeClass, useSurfaceTheme } from "@/auth/useSurfaceTheme";
import { Button, Textarea } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";

type Pending = {
  message: string;
  resolve: (value: string | null) => void;
};

/**
 * P42: when the API refuses an operator's MAJOR action (pause, suspend, delete or disable a
 * workspace, account or feature) with `ops_reason_required`, this dialog asks why - the answer
 * is kept in the audit log with the operator's name - and hands it back to the client to retry.
 */
export function OpsReasonDialog() {
  // Same shared theme store StepUpDialog reads, so the operator prompt matches every other
  // security surface in the console when the sidebar toggle flips the theme.
  const { theme } = useSurfaceTheme();
  const { api } = useAuth();
  const [pending, setPending] = React.useState<Pending | null>(null);
  const [text, setText] = React.useState("");
  const textareaRef = React.useRef<HTMLTextAreaElement | null>(null);
  // Mirrors pending.resolve so the effect cleanup (unmount, or an api swap) can settle the
  // promise without reading state React may already have torn down.
  const resolveRef = React.useRef<((value: string | null) => void) | null>(null);

  React.useEffect(() => {
    api.onReasonRequired = (details) =>
      new Promise<string | null>((resolve) => {
        resolveRef.current = resolve;
        setText("");
        setPending({ message: details.message, resolve });
      });
    return () => {
      api.onReasonRequired = undefined;
      const resolve = resolveRef.current;
      resolveRef.current = null;
      if (resolve) resolve(null);
      setPending(null);
    };
  }, [api]);

  React.useEffect(() => {
    if (pending) textareaRef.current?.focus();
  }, [pending]);

  React.useEffect(() => {
    if (!pending) return;
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") close(null);
    }
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [pending]);

  function close(value: string | null) {
    const resolve = resolveRef.current;
    resolveRef.current = null;
    setPending(null);
    if (resolve) resolve(value);
  }

  function submit() {
    const value = text.trim();
    if (value.length < 5) return;
    close(value);
  }

  if (!pending) return null;

  return (
    <div
      role="dialog"
      aria-modal="true"
      aria-labelledby="ops-reason-title"
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/60 p-4 backdrop-blur-[2px]"
    >
      <div className={cn("auth-surface", surfaceThemeClass(theme), "w-full max-w-md")}>
        <form
          onSubmit={(event) => {
            event.preventDefault();
            submit();
          }}
          className="w-full rounded-[var(--cx-r-md,14px)] border border-border bg-background p-5 shadow-lg"
        >
          <h2 id="ops-reason-title" className="text-sm font-semibold">
            Why are you doing this?
          </h2>
          <p className="mt-1 text-xs leading-relaxed text-muted-foreground">
            {pending.message}
          </p>
          <div className="mt-4">
            <Textarea
              ref={textareaRef}
              aria-label="Reason"
              maxLength={500}
              value={text}
              onChange={(event) => setText(event.target.value)}
              onKeyDown={(event) => {
                if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
                  event.preventDefault();
                  submit();
                }
              }}
              className="min-h-[80px]"
            />
            <p className="mt-1 text-xs text-muted-foreground">
              Kept in the operator audit log with your name.
            </p>
          </div>
          <div className="mt-5 flex justify-end gap-2">
            <Button type="button" variant="outline" onClick={() => close(null)}>
              Cancel
            </Button>
            <Button type="submit" disabled={text.trim().length < 5}>
              Continue
            </Button>
          </div>
        </form>
      </div>
    </div>
  );
}
