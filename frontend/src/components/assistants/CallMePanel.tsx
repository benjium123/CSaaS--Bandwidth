import * as React from "react";
import { useMutation } from "@tanstack/react-query";
import { useAuth } from "@/auth/AuthContext";
import { callMe, looksLikePhoneNumber, toE164 } from "@/api/assistantOps";
import { Button, Input, MutationStatus } from "@/components/ui/primitives";

export function CallMePanel({
  assistantId,
  defaultPhone,
}: {
  assistantId: string | null;
  defaultPhone?: string | null;
}): React.JSX.Element {
  const { api } = useAuth();
  const [value, setValue] = React.useState(defaultPhone ?? "");

  const mutation = useMutation({
    mutationFn: async () => {
      if (assistantId === null) {
        throw new Error("Save this assistant first.");
      }
      return callMe(api, assistantId, toE164(value));
    },
  });

  const missingAssistant = assistantId === null;
  const invalidPhone = !looksLikePhoneNumber(value);
  const disabled = missingAssistant || invalidPhone || mutation.isPending;

  return (
    <div className="space-y-3">
      <p className="text-sm text-muted-foreground">
        Have your assistant call you so you can hear it for real.
      </p>
      <Input
        id="call-me-phone"
        aria-label="Your phone number"
        value={value}
        onChange={(event) => setValue(event.target.value)}
        placeholder="(555) 123-4567"
        type="tel"
        disabled={mutation.isPending}
      />
      <div className="flex items-center gap-3">
        <Button
          type="button"
          disabled={disabled}
          onClick={() => mutation.mutate()}
        >
          Call me
        </Button>
        {missingAssistant ? (
          <span className="text-sm text-muted-foreground">
            Save this assistant first.
          </span>
        ) : null}
        {mutation.status !== "idle" && (
          <MutationStatus
            pending={mutation.isPending}
            error={mutation.error}
            success="Calling you now — pick up."
            pendingLabel="Calling…"
          />
        )}
      </div>
      <p className="text-xs text-muted-foreground">
        This is a test call and is not counted in your reports.
      </p>
    </div>
  );
}
