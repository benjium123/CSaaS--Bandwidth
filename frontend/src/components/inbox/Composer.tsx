import * as React from "react";
import { ApiError } from "@/api/client";
import { estimateSmsSegments } from "@/lib/format";
import { Button } from "@/components/ui/primitives";

/**
 * Compose + send.
 *
 * Two P1/P2 contracts show up here as UI:
 *  - a 201 with status "rejected" is DATA, not an error - it renders as a failed bubble
 *    with its carrier code, handled by the thread view.
 *  - a 422 sticky_sender_unavailable means this conversation's number was retired. We do
 *    NOT silently resend from another number; we ask, then retry with allow_reassign.
 */
export function Composer({
  onSend,
  disabled,
}: {
  onSend: (body: string, allowReassign: boolean) => Promise<void>;
  disabled?: boolean;
}) {
  const [body, setBody] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const [needsReassign, setNeedsReassign] = React.useState(false);
  const reassignButtonRef = React.useRef<HTMLButtonElement>(null);

  // Item 52: move focus into the reassign prompt the moment it appears - a sighted
  // mouse user sees it pop up right above the composer, but a keyboard/screen-reader
  // user's focus is still sitting in the message field with no cue anything changed.
  React.useEffect(() => {
    if (needsReassign) reassignButtonRef.current?.focus();
  }, [needsReassign]);

  const segments = React.useMemo(() => estimateSmsSegments(body), [body]);

  async function submit(allowReassign: boolean) {
    if (!body.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await onSend(body.trim(), allowReassign);
      setBody("");
      setNeedsReassign(false);
    } catch (err) {
      if (err instanceof ApiError && err.code === "sticky_sender_unavailable") {
        setNeedsReassign(true);
      } else {
        setError((err as Error).message);
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="space-y-2 border-t border-border p-3">
      {needsReassign && (
        <div
          role="alert"
          className="flex items-center justify-between gap-3 rounded-md border border-border bg-muted p-2 text-xs"
        >
          <span>This conversation&rsquo;s number was retired. Send from a new number?</span>
          <div className="flex gap-2">
            <Button size="sm" variant="ghost" onClick={() => setNeedsReassign(false)}>
              Cancel
            </Button>
            <Button ref={reassignButtonRef} size="sm" onClick={() => submit(true)}>
              Send anyway
            </Button>
          </div>
        </div>
      )}

      {error && (
        <p role="alert" className="text-xs text-destructive">
          {error}
        </p>
      )}

      <form
        className="flex items-end gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          void submit(false);
        }}
      >
        <div className="flex-1 space-y-1">
          <textarea
            aria-label="Message"
            placeholder="Type a message"
            value={body}
            disabled={disabled || busy}
            rows={1}
            onChange={(e) => setBody(e.target.value)}
            onKeyDown={(e) => {
              // Item 51: Enter sends, Shift+Enter inserts a newline - the usual chat
              // convention. IME composition (e.g. typing accented/CJK text) must not
              // trigger a send on the Enter that merely confirms the composition.
              if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault();
                void submit(false);
              }
            }}
            className="flex max-h-40 min-h-9 w-full resize-y rounded-md border border-border bg-background px-3 py-2 text-sm shadow-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-2"
          />
          <p className="text-[11px] text-muted-foreground">
            {segments.units} char{segments.units === 1 ? "" : "s"} · {segments.encoding} ·{" "}
            {segments.segments} segment{segments.segments === 1 ? "" : "s"}
          </p>
        </div>
        <Button type="submit" disabled={disabled || busy || !body.trim()}>
          Send
        </Button>
      </form>
    </div>
  );
}
