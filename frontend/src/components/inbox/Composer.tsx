import * as React from "react";
import { ApiError } from "@/api/client";
import { Button, Input } from "@/components/ui/primitives";

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
            <Button size="sm" onClick={() => submit(true)}>
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
        className="flex gap-2"
        onSubmit={(e) => {
          e.preventDefault();
          void submit(false);
        }}
      >
        <Input
          aria-label="Message"
          placeholder="Type a message"
          value={body}
          disabled={disabled || busy}
          onChange={(e) => setBody(e.target.value)}
        />
        <Button type="submit" disabled={disabled || busy || !body.trim()}>
          Send
        </Button>
      </form>
    </div>
  );
}
