import { Check } from "lucide-react";
import { useNavigate } from "react-router-dom";
import { useGate } from "@/api/capabilities";
import { Button, Card, Pill, Section } from "@/components/ui/primitives";

type Step = {
  label: string;
  to: string;
  done: boolean;
  hint?: string;
};

export function OnboardingChecklist() {
  const gate = useGate();
  const navigate = useNavigate();
  const org = gate.org;

  if (!org) return null;

  const fullySetUp =
    org.has_provider &&
    org.has_number &&
    org.member_count > 1 &&
    org.registration_state !== "none";

  if (fullySetUp) return null;

  const steps: Step[] = [
    { label: "Connect a provider", to: "/settings/providers", done: org.has_provider },
    { label: "Get a phone number", to: "/settings/numbers", done: org.has_number },
    { label: "Invite your team", to: "/settings/team", done: org.member_count > 1 },
    {
      label: "Register for texting",
      to: "/settings/numbers",
      done: org.registration_state !== "none",
      hint: "Carriers require this before you can text. It takes a few days — we track it for you.",
    },
  ];

  return (
    <Card>
      <Section title="Finish setting up" description="Four steps and you are live.">
        <ol aria-label="Setup steps" className="space-y-3">
          {steps.map((step, index) => (
            <li
              key={step.label}
              aria-label={`${step.label}${step.done ? " done" : ""}`}
              className="flex items-start gap-3"
            >
              {step.done ? (
                <Pill tone="success" aria-hidden="true">
                  <Check className="h-3.5 w-3.5" />
                </Pill>
              ) : (
                <span className="flex h-6 w-6 shrink-0 items-center justify-center rounded-full bg-muted text-xs text-muted-foreground">
                  {index + 1}
                </span>
              )}

              <div className="min-w-0 flex-1">
                <Button
                  type="button"
                  variant="outline"
                  size="sm"
                  onClick={() => navigate(step.to)}
                >
                  {step.label}
                </Button>
                {step.hint ? (
                  <p className="mt-1 text-xs text-muted-foreground">{step.hint}</p>
                ) : null}
              </div>
            </li>
          ))}
        </ol>
      </Section>
    </Card>
  );
}
