import * as React from "react";
import { Button, Card, Pill, Textarea } from "@/components/ui/primitives";

/** P43: the AI's prepared decision on a business application. The AI recommends; the
 * operator decides with one click (approval blockers still apply on the server). */

export type DecisionPack = {
  recommendation: "approve" | "needs_info" | "reject";
  confidence: number;
  summary: string;
  thoughts: { area: string; assessment: string; thought: string }[];
  concerns: { concern: string; evidence: string }[];
  questions_for_applicant: string[];
  suggested_risk: "standard" | "high";
  suggested_limits: { daily_calls: number | null; daily_texts: number | null; max_numbers: number | null };
  note_for_decision: string;
  error?: string;
};

const RECOMMENDATION: Record<string, { label: string; tone: "success" | "warning" | "danger" }> = {
  approve: { label: "AI recommends: approve", tone: "success" },
  needs_info: { label: "AI recommends: ask for more info", tone: "warning" },
  reject: { label: "AI recommends: reject", tone: "danger" },
};

const ASSESSMENT_TONE: Record<string, "success" | "warning" | "neutral"> = {
  ok: "success",
  concern: "warning",
  unclear: "neutral",
};

export function DecisionPackCard({
  pack,
  result,
  blockers,
  pending,
  onApprove,
  onAskInfo,
  onReject,
}: {
  pack: DecisionPack | null;
  result: string | null;
  blockers: string[];
  pending: boolean;
  onApprove: (note: string, limits: DecisionPack["suggested_limits"]) => void;
  onAskInfo: (message: string) => void;
  onReject: (reason: string, ban: boolean) => void;
}) {
  const questions = (pack?.questions_for_applicant ?? []).join("\n");
  const [message, setMessage] = React.useState(questions);
  const [ban, setBan] = React.useState(false);
  React.useEffect(() => setMessage(questions), [questions]);

  if (!pack || result === "error") {
    return (
      <Card>
        <p className="text-sm font-medium">AI decision</p>
        <p className="text-sm text-muted-foreground">
          {result === "error"
            ? "The AI couldn't prepare a decision yet - it retries automatically, or use Re-run checks."
            : "The AI is still reading this application. It usually takes a minute or two after submission."}
        </p>
      </Card>
    );
  }
  const rec = RECOMMENDATION[pack.recommendation];
  const limits = pack.suggested_limits;
  const limitsText = [
    limits.daily_calls != null ? `${limits.daily_calls} calls/day` : null,
    limits.daily_texts != null ? `${limits.daily_texts} texts/day` : null,
    limits.max_numbers != null ? `${limits.max_numbers} numbers` : null,
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <Card className="space-y-[14px] border-[hsl(var(--cx-accent)/0.4)]">
      <div className="flex flex-wrap items-center gap-[11px]">
        <Pill tone={rec.tone}>{rec.label}</Pill>
        <span className="text-sm text-muted-foreground">{pack.confidence}% confident</span>
        {pack.suggested_risk === "high" && <Pill tone="danger">AI sees high risk</Pill>}
      </div>
      <p className="text-sm">{pack.summary}</p>

      {pack.thoughts.length > 0 && (
        <div className="space-y-1">
          <p className="text-xs font-medium text-muted-foreground">How it thought about it</p>
          <ul className="space-y-1">
            {pack.thoughts.map((t, i) => (
              <li key={`${t.area}-${i}`} className="flex flex-wrap items-center gap-[11px] text-sm">
                <Pill tone={ASSESSMENT_TONE[t.assessment] ?? "neutral"}>{t.area.replace(/_/g, " ")}</Pill>
                <span>{t.thought}</span>
              </li>
            ))}
          </ul>
        </div>
      )}

      {pack.concerns.length > 0 && (
        <div className="space-y-1">
          <p className="text-xs font-medium text-muted-foreground">Concerns</p>
          <ul className="list-disc space-y-1 pl-5 text-sm text-[hsl(var(--cx-flag))]">
            {pack.concerns.map((c, i) => (
              <li key={i}>
                {c.concern}
                {c.evidence && <span className="text-muted-foreground"> — {c.evidence}</span>}
              </li>
            ))}
          </ul>
        </div>
      )}

      {limitsText && <p className="text-xs text-muted-foreground">Suggested starting limits: {limitsText}</p>}
      {blockers.length > 0 && (
        <ul className="list-disc pl-5 text-sm text-[hsl(var(--cx-flag))]">
          {blockers.map((b) => (
            <li key={b}>{b}</li>
          ))}
        </ul>
      )}

      <div className="flex flex-wrap gap-[11px]">
        <Button
          type="button"
          disabled={pending || blockers.length > 0}
          onClick={() => onApprove(pack.note_for_decision || pack.summary, limits)}
        >
          Approve{pack.recommendation === "approve" ? " as recommended" : ""}
        </Button>
        <Button type="button" variant="destructive" disabled={pending} onClick={() => onReject(pack.note_for_decision || pack.summary, ban)}>
          Reject{pack.recommendation === "reject" ? " as recommended" : ""}
        </Button>
        <label className="flex items-center gap-2 text-sm">
          <input type="checkbox" checked={ban} onChange={(e) => setBan(e.target.checked)} />
          also ban identifiers
        </label>
      </div>
      <div className="space-y-[11px]">
        <Textarea
          aria-label="Questions for the applicant"
          rows={Math.max(2, (pack.questions_for_applicant ?? []).length + 1)}
          value={message}
          onChange={(e) => setMessage(e.target.value)}
          placeholder="What should the business send or explain?"
        />
        <Button type="button" variant="outline" disabled={pending || !message.trim()} onClick={() => onAskInfo(message)}>
          Ask for more info{pack.recommendation === "needs_info" ? " as recommended" : ""}
        </Button>
      </div>
    </Card>
  );
}
