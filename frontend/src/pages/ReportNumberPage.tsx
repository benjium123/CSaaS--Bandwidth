import * as React from "react";
import { useMutation } from "@tanstack/react-query";
import { useAuth } from "@/auth/AuthContext";
import { Button, Card, Input, Select, Textarea, mutationErrorMessage } from "@/components/ui/primitives";

/** P43: anyone who received a suspicious call or text from one of our numbers can report it.
 * No account needed. The answer is the same whether or not the number is ours. */
export function ReportNumberPage() {
  const { api } = useAuth();
  const [number, setNumber] = React.useState("");
  const [kind, setKind] = React.useState<"call" | "text">("call");
  const [description, setDescription] = React.useState("");
  const [contact, setContact] = React.useState("");
  const send = useMutation({
    mutationFn: () =>
      api.request("/api/v1/public/report-number", {
        method: "POST",
        json: { number, kind, description, contact: contact || null },
      }),
  });

  return (
    <div className="flex min-h-full items-center justify-center p-6">
      <Card className="w-full max-w-lg space-y-4">
        <div>
          <h1 className="text-lg font-semibold">Report a suspicious call or text</h1>
          <p className="text-sm text-muted-foreground">
            Got a call or text that looked like a scam from a number on our network? Tell us and our
            safety team will look into it. You don't need an account.
          </p>
        </div>
        {send.isSuccess ? (
          <p role="status" className="text-sm">
            Thank you - your report was received. If you shared a way to reach you, we may follow up.
            If you lost money, also contact your bank and local police.
          </p>
        ) : (
          <form
            className="space-y-3"
            onSubmit={(e) => {
              e.preventDefault();
              send.mutate();
            }}
          >
            <label className="block space-y-1">
              <span className="text-sm">The number that contacted you</span>
              <Input aria-label="Number that contacted you" inputMode="tel" placeholder="+1 512 555 0100" value={number} onChange={(e) => setNumber(e.target.value)} />
            </label>
            <label className="block space-y-1">
              <span className="text-sm">It was a</span>
              <Select aria-label="Call or text" value={kind} onChange={(e) => setKind(e.target.value as "call" | "text")}>
                <option value="call">Phone call</option>
                <option value="text">Text message</option>
              </Select>
            </label>
            <label className="block space-y-1">
              <span className="text-sm">What happened</span>
              <Textarea aria-label="What happened" rows={4} placeholder="What did they say or ask for?" value={description} onChange={(e) => setDescription(e.target.value)} />
            </label>
            <label className="block space-y-1">
              <span className="text-sm">How to reach you (optional)</span>
              <Input aria-label="How to reach you" placeholder="Email or phone" value={contact} onChange={(e) => setContact(e.target.value)} />
            </label>
            <Button type="submit" disabled={number.trim().length < 7 || description.trim().length < 10 || send.isPending}>
              Send report
            </Button>
            {send.isError && <p role="alert" className="text-sm text-destructive">{mutationErrorMessage(send.error)}</p>}
          </form>
        )}
      </Card>
    </div>
  );
}
