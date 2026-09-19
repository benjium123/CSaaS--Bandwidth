import * as React from "react";
import { useMutation } from "@tanstack/react-query";
import { useAuth } from "@/auth/AuthContext";
import { Select, Textarea, mutationErrorMessage } from "@/components/ui/primitives";
import {
  AuthAlert,
  AuthButton,
  AuthInput,
  AuthPlate,
  AuthSurface,
  Field,
} from "@/components/auth/AuthShell";

/**
 * P43: anyone who received a suspicious call or text from one of our numbers can report it.
 * No account needed. The answer is the same whether or not the number is ours.
 *
 * REGISTER. This is the one route in the unauthenticated table that is not an auth screen.
 * The person reading it was just cold-called, has no account and owes us nothing, so it
 * deliberately wears none of the sign-in furniture: no product pitch, no workspace panel,
 * no invitation to become a user. `aside={null}` drops the marketing column entirely. It
 * is also mounted INSIDE the signed-in shell on /report, so it has to look right embedded
 * as well as standing alone - which is the other reason there is no full-page identity
 * column here.
 */
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
    <AuthSurface aside={null}>
      <AuthPlate
        as="form"
        onSubmit={(e) => {
          e.preventDefault();
          send.mutate();
        }}
        eyebrow="Trust & safety"
        title="Report a suspicious call or text"
        lede="Got a call or text that looked like a scam from a number on our network? Tell us and our safety team will look into it. You don't need an account."
      >
        {send.isSuccess ? (
          <p role="status" className="ex-notice">
            Thank you - your report was received. If you shared a way to reach you, we may follow
            up. If you lost money, also contact your bank and local police.
          </p>
        ) : (
          <div className="space-y-4">
            <Field label="The number that contacted you">
              <AuthInput
                aria-label="Number that contacted you"
                inputMode="tel"
                placeholder="+1 512 555 0100"
                value={number}
                onChange={(e) => setNumber(e.target.value)}
              />
            </Field>
            <Field label="It was a">
              <Select
                aria-label="Call or text"
                className="ex-input"
                value={kind}
                onChange={(e) => setKind(e.target.value as "call" | "text")}
              >
                <option value="call">Phone call</option>
                <option value="text">Text message</option>
              </Select>
            </Field>
            <Field label="What happened">
              <Textarea
                aria-label="What happened"
                className="ex-input h-auto py-2.5"
                rows={4}
                placeholder="What did they say or ask for?"
                value={description}
                onChange={(e) => setDescription(e.target.value)}
              />
            </Field>
            <Field
              label="How to reach you (optional)"
              hint="Only used to follow up on this report."
            >
              <AuthInput
                aria-label="How to reach you"
                placeholder="Email or phone"
                value={contact}
                onChange={(e) => setContact(e.target.value)}
              />
            </Field>
            <AuthButton
              type="submit"
              block
              disabled={
                number.trim().length < 7 ||
                description.trim().length < 10 ||
                send.isPending
              }
            >
              Send report
            </AuthButton>
            {send.isError && <AuthAlert>{mutationErrorMessage(send.error)}</AuthAlert>}
          </div>
        )}
      </AuthPlate>
    </AuthSurface>
  );
}
