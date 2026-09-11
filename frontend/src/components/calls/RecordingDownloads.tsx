import * as React from "react";
import type { ApiClient } from "@/api/client";
import { fetchAuthedBlob } from "@/api/client";
import {
  RECORDING_LAYOUT_WORDS,
  recordingFiles,
  type RecordingFile,
  type RecordingLayout,
} from "@/api/calls";
import type { RecordingOut } from "@/api/hooks";
import { Button } from "@/components/ui/primitives";

function extensionFor(contentType: string): string {
  if (contentType === "audio/wav" || contentType === "audio/x-wav" || contentType === "audio/wave") {
    return "wav";
  }
  if (contentType === "audio/mpeg") return "mp3";
  if (contentType === "audio/ogg") return "ogg";
  return "audio";
}

export function RecordingDownloads({
  api,
  callId,
  recording,
}: {
  api: ApiClient;
  callId: string;
  recording: RecordingOut;
}) {
  const files = recordingFiles(recording);
  const [pendingLayouts, setPendingLayouts] = React.useState<
    Partial<Record<RecordingLayout, boolean>>
  >({});
  const [error, setError] = React.useState<string | null>(null);

  if (files.length === 0) return null;

  function setPending(layout: RecordingLayout, value: boolean) {
    setPendingLayouts((previous) => ({ ...previous, [layout]: value }));
  }

  async function download(file: RecordingFile) {
    setError(null);
    setPending(file.layout, true);
    try {
      const blob = await fetchAuthedBlob(api, file.url);
      const objectUrl = URL.createObjectURL(blob);
      const anchor = document.createElement("a");
      anchor.href = objectUrl;
      anchor.download = `call-${callId}-${file.layout}.${extensionFor(recording.content_type)}`;
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      setTimeout(() => URL.revokeObjectURL(objectUrl), 0);
    } catch (err) {
      setError((err as Error).message);
    } finally {
      setPending(file.layout, false);
    }
  }

  if (files.length === 1) {
    const file = files[0];
    return (
      <div className="flex flex-wrap items-center gap-2">
        <Button
          type="button"
          variant="outline"
          size="sm"
          onClick={() => download(file)}
          disabled={Boolean(pendingLayouts[file.layout])}
        >
          Download
        </Button>
        {error ? (
          <span role="alert" className="text-xs text-destructive">
            {error}
          </span>
        ) : null}
      </div>
    );
  }

  return (
    <div className="flex flex-wrap items-center gap-2 text-sm">
      <span className="text-muted-foreground">Download</span>
      {files.map((file) => {
        const words = RECORDING_LAYOUT_WORDS[file.layout];
        return (
          <Button
            key={file.layout}
            type="button"
            variant="outline"
            size="sm"
            aria-label={`Download ${words.toLowerCase()}`}
            onClick={() => download(file)}
            disabled={Boolean(pendingLayouts[file.layout])}
          >
            {words}
          </Button>
        );
      })}
      {error ? (
        <span role="alert" className="text-xs text-destructive">
          {error}
        </span>
      ) : null}
    </div>
  );
}
