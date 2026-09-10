import * as React from "react";
import { useAuth } from "@/auth/AuthContext";
import { previewVoice } from "@/api/assistantOps";
import { Button } from "@/components/ui/primitives";

export function VoicePreviewButton({
  ttsProvider,
  voiceId,
}: {
  ttsProvider: string;
  voiceId: string;
}): React.JSX.Element {
  const { api } = useAuth();
  const [audioUrl, setAudioUrl] = React.useState<string | null>(null);
  const [loading, setLoading] = React.useState(false);
  const [error, setError] = React.useState<string | null>(null);
  const audioRef = React.useRef<HTMLAudioElement | null>(null);
  const currentKeyRef = React.useRef(`${ttsProvider}\u0000${voiceId}`);

  React.useEffect(() => {
    currentKeyRef.current = `${ttsProvider}\u0000${voiceId}`;
  }, [ttsProvider, voiceId]);

  React.useEffect(() => {
    if (audioUrl) {
      audioRef.current?.play();
    }
  }, [audioUrl]);

  React.useEffect(() => {
    return () => {
      if (audioUrl) {
        URL.revokeObjectURL(audioUrl);
      }
    };
  }, [audioUrl]);

  React.useEffect(() => {
    setAudioUrl(null);
    setError(null);
    setLoading(false);
  }, [ttsProvider, voiceId]);

  const missingVoiceId = voiceId.trim() === "";

  async function handlePreview() {
    const requestKey = `${ttsProvider}\u0000${voiceId}`;
    setLoading(true);
    setError(null);
    try {
      const blob = await previewVoice(api, {
        tts_provider: ttsProvider,
        voice_id: voiceId,
      });
      if (currentKeyRef.current !== requestKey) return;
      setAudioUrl(URL.createObjectURL(blob));
    } catch (err) {
      if (currentKeyRef.current !== requestKey) return;
      setError(err instanceof Error ? err.message : "Something went wrong.");
    } finally {
      if (currentKeyRef.current === requestKey) {
        setLoading(false);
      }
    }
  }

  return (
    <div className="flex items-center gap-2">
      <Button
        type="button"
        aria-label="Preview voice"
        disabled={missingVoiceId || loading}
        onClick={handlePreview}
      >
        {loading ? "Playing…" : "Preview voice"}
      </Button>
      {missingVoiceId ? (
        <span className="text-xs text-muted-foreground">
          Add a voice id first.
        </span>
      ) : null}
      {audioUrl ? (
        <audio ref={audioRef} controls className="h-8" src={audioUrl} />
      ) : null}
      {error ? (
        <span role="alert" className="text-xs text-destructive">
          {error}
        </span>
      ) : null}
    </div>
  );
}
