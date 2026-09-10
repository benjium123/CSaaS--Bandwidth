import * as React from "react";
import { Play, Sparkles } from "lucide-react";
import { fetchAuthedBlob, type ApiClient } from "@/api/client";
import { useCall } from "@/api/hooks";
import {
  dispositionTone,
  dispositionWords,
  sentimentWords,
  type AssistantCallSummary,
} from "@/api/assistantOps";
import { Button, Pill, Spinner } from "@/components/ui/primitives";

export interface AiCallCardItem {
  id: string;
  direction: "inbound" | "outbound";
  status: string;
  duration_seconds: number | null;
  occurred_at: string;
  failure_detail: string | null;
  recording: { id: string; status: string; duration_seconds: number | null } | null;
  assistant: AssistantCallSummary;
}

/**
 * Deliberate copy of Timeline's relative-time helper. Keeping a private copy here avoids
 * a circular import with Timeline, which is already wired to render this card.
 */
function relativeTime(iso: string): string {
  const then = new Date(iso).getTime();
  const deltaSeconds = Math.round((then - Date.now()) / 1000);
  const formatter = new Intl.RelativeTimeFormat("en-US", { numeric: "auto" });
  const abs = Math.abs(deltaSeconds);

  if (abs < 60) return formatter.format(deltaSeconds, "second");

  const deltaMinutes = Math.round(deltaSeconds / 60);
  if (Math.abs(deltaMinutes) < 60) return formatter.format(deltaMinutes, "minute");

  const deltaHours = Math.round(deltaSeconds / 3600);
  if (Math.abs(deltaHours) < 24) return formatter.format(deltaHours, "hour");

  return formatter.format(Math.round(deltaSeconds / 86400), "day");
}

function formatDuration(totalSeconds: number): string {
  const m = Math.floor(totalSeconds / 60);
  const s = totalSeconds % 60;
  return `${m}:${String(s).padStart(2, "0")}`;
}

export function AiCallCard({
  item,
  api,
}: {
  item: AiCallCardItem;
  api: ApiClient;
}): React.JSX.Element {
  const [expanded, setExpanded] = React.useState(false);
  const transcriptId = React.useId();
  const [audioUrl, setAudioUrl] = React.useState<string | null>(null);
  const [recordingLoading, setRecordingLoading] = React.useState(false);
  const [recordingError, setRecordingError] = React.useState<string | null>(null);
  const audioRef = React.useRef<HTMLAudioElement>(null);

  // Disabled until expanded, so nothing is fetched on initial render.
  const transcriptQuery = useCall(api, expanded ? item.id : null);

  React.useEffect(() => {
    return () => {
      if (audioUrl) URL.revokeObjectURL(audioUrl);
    };
  }, [audioUrl]);

  React.useEffect(() => {
    if (audioUrl) void audioRef.current?.play();
  }, [audioUrl]);

  async function playRecording() {
    setRecordingError(null);

    if (audioUrl) {
      void audioRef.current?.play();
      return;
    }

    if (!item.recording) return;

    setRecordingLoading(true);
    try {
      const blob = await fetchAuthedBlob(
        api,
        `/api/v1/calls/${item.id}/recordings/${item.recording.id}`,
      );
      setAudioUrl(URL.createObjectURL(blob));
    } catch (err) {
      setRecordingError((err as Error).message);
    } finally {
      setRecordingLoading(false);
    }
  }

  const dispositionLabel = dispositionWords(item.assistant.disposition);
  const sentimentLabel = sentimentWords(item.assistant.sentiment);
  const hasDuration = item.duration_seconds != null && item.duration_seconds > 0;
  // The duration is not a pill, but it shares the pill row - it must not disappear just
  // because the call carried no outcome and no sentiment.
  const hasPills = Boolean(dispositionLabel || sentimentLabel) || hasDuration;
  const assistantName = item.assistant.name ? ` — ${item.assistant.name}` : "";
  const title =
    item.direction === "inbound"
      ? `Assistant answered${assistantName}`
      : `Assistant called${assistantName}`;
  const summary =
    item.assistant.summary && item.assistant.summary.trim() ? item.assistant.summary : null;

  return (
    <div className="rounded-lg border border-border bg-background p-3 text-sm text-foreground">
      <div className="flex items-center gap-2">
        <Sparkles className="h-4 w-4 shrink-0 text-muted-foreground" />
        <span className="font-medium">{title}</span>
        <span className="ml-auto shrink-0 text-[11px] text-muted-foreground">
          {relativeTime(item.occurred_at)}
        </span>
      </div>

      {hasPills && (
        <div className="mt-2 flex flex-wrap items-center gap-2">
          {dispositionLabel && (
            <Pill tone={dispositionTone(item.assistant.disposition)}>{dispositionLabel}</Pill>
          )}
          {sentimentLabel && <Pill tone="neutral">{sentimentLabel}</Pill>}
          {hasDuration && (
            <span className="text-xs text-muted-foreground">
              {formatDuration(item.duration_seconds as number)}
            </span>
          )}
        </div>
      )}

      {summary ? (
        <p className="mt-2 text-sm text-foreground">{summary}</p>
      ) : (
        <p className="mt-2 text-sm text-muted-foreground">No summary for this call.</p>
      )}

      {item.failure_detail && (
        <p role="alert" className="mt-2 text-xs text-destructive">
          {item.failure_detail}
        </p>
      )}

      {(item.recording || item.assistant.has_transcript) && (
        <div className="mt-2 flex flex-wrap items-center gap-2">
          {item.recording && (
            <>
              <Button
                type="button"
                variant="outline"
                size="sm"
                onClick={playRecording}
                disabled={recordingLoading || item.recording.status !== "stored"}
                aria-label="Play recording"
              >
                <Play className="mr-1 h-3 w-3" />
                {recordingLoading ? "Loading…" : "Play recording"}
              </Button>
              {audioUrl && <audio ref={audioRef} src={audioUrl} controls className="h-8" />}
              {recordingError && (
                <span role="alert" className="text-xs text-destructive">
                  {recordingError}
                </span>
              )}
            </>
          )}

          {item.assistant.has_transcript && (
            <Button
              type="button"
              variant="ghost"
              size="sm"
              aria-label={expanded ? "Hide transcript" : "Show transcript"}
              aria-expanded={expanded}
              aria-controls={transcriptId}
              onClick={() => setExpanded((current) => !current)}
            >
              {expanded ? "Hide transcript" : "Show transcript"}
            </Button>
          )}
        </div>
      )}

      {expanded && (
        <div id={transcriptId}>
          {transcriptQuery.isLoading ? (
            <Spinner label="Loading transcript" />
          ) : transcriptQuery.error ? (
            <p role="alert" className="text-xs text-destructive">
              {(transcriptQuery.error as Error).message}
            </p>
          ) : transcriptQuery.data?.transcript?.length ? (
            <ol className="mt-2 space-y-1">
              {transcriptQuery.data.transcript.map((entry, index) => (
                <li key={`${entry.at_ms}-${index}`} className="text-xs">
                  <span className="font-medium">
                    {entry.role === "assistant" ? "Assistant" : "Caller"}
                  </span>{" "}
                  {entry.text}
                </li>
              ))}
            </ol>
          ) : (
            <p className="mt-2 text-xs text-muted-foreground">No transcript for this call.</p>
          )}
        </div>
      )}
    </div>
  );
}
