import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { ApiClient } from "@/api/client";
import { fetchInboxes } from "@/api/conversations";
import type { CallDetailOut, RecordingOut } from "@/api/hooks";

export type RecordingLayout = "mixed" | "agent" | "customer";
export type ChannelLayout = "mixed" | "dual";

export interface CallingSettings {
  recording_announcement: boolean;
  recording_announcement_text: string | null;
  announcement_text_effective: string;
  channel_layout: ChannelLayout;
  dispositions: string[];
}

export type CallingSettingsPatch = Partial<
  Pick<
    CallingSettings,
    | "recording_announcement"
    | "recording_announcement_text"
    | "channel_layout"
    | "dispositions"
  >
>;

export const MAX_DISPOSITIONS = 25;
export const MAX_DISPOSITION_LEN = 32;
export const MAX_ANNOUNCEMENT_LEN = 500;
export const MAX_DISPOSITION_NOTE_LEN = 2000;

export const CALLING_SETTINGS_KEY = ["calling-settings"] as const;

export function useCallingSettings(api: ApiClient, enabled = true) {
  return useQuery({
    queryKey: CALLING_SETTINGS_KEY,
    queryFn: () => api.request<CallingSettings>("/api/v1/orgs/current/calling"),
    enabled,
  });
}

export function useUpdateCallingSettings(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (patch: CallingSettingsPatch) =>
      api.request<CallingSettings>("/api/v1/orgs/current/calling", {
        method: "PATCH",
        json: patch,
      }),
    onSuccess: (data) => {
      qc.setQueryData(CALLING_SETTINGS_KEY, data);
      void qc.invalidateQueries({ queryKey: CALL_DISPOSITIONS_KEY });
    },
  });
}

export const CALL_DISPOSITIONS_KEY = ["call-dispositions"] as const;

/** The org's call-result list, readable with calls:read: agents record call results
 * but cannot read the rest of the calling settings (settings:read). */
export function useCallDispositions(api: ApiClient, enabled = true) {
  return useQuery({
    queryKey: CALL_DISPOSITIONS_KEY,
    queryFn: () => api.request<{ dispositions: string[] }>("/api/v1/calls/dispositions"),
    enabled,
  });
}

export function validateDispositions(list: string[]): string | null {
  if (list.length === 0) return "Call results cannot be empty";
  if (list.length > MAX_DISPOSITIONS) return "You can add at most 25 call results";

  const trimmed = list.map((value) => value.trim());
  const seen = new Set<string>();

  for (const value of trimmed) {
    if (!value) return "Call results cannot be blank";
    if (value.length > MAX_DISPOSITION_LEN) {
      return "A call result can be at most 32 characters";
    }
    const key = value.toLowerCase();
    if (seen.has(key)) return "Call results must be unique";
    seen.add(key);
  }

  return null;
}

export function normalizeDispositions(list: string[]): string[] {
  return list.map((value) => value.trim());
}

export interface CallDisposition {
  disposition: string | null;
  disposition_note: string | null;
}

function readString(value: unknown): string | null {
  return typeof value === "string" ? value : null;
}

export function dispositionOf(call: object | null | undefined): CallDisposition {
  const record = call as unknown as Record<string, unknown> | null | undefined;
  return {
    disposition: readString(record?.["disposition"]),
    disposition_note: readString(record?.["disposition_note"]),
  };
}

export interface RecordingFile {
  layout: RecordingLayout;
  url: string;
}

export function recordingFiles(recording: RecordingOut): RecordingFile[] {
  const candidate = recording as RecordingOut & { files?: unknown };
  if (!Array.isArray(candidate.files)) return [];

  const files: RecordingFile[] = [];
  const order: Record<RecordingLayout, number> = {
    mixed: 0,
    agent: 1,
    customer: 2,
  };

  for (const entry of candidate.files) {
    if (!entry || typeof entry !== "object") continue;
    const file = entry as { layout?: unknown; url?: unknown };
    const layout = file.layout;
    if (
      (layout === "mixed" || layout === "agent" || layout === "customer") &&
      typeof file.url === "string"
    ) {
      files.push({ layout, url: file.url });
    }
  }

  return files.sort((a, b) => order[a.layout] - order[b.layout]);
}

export const RECORDING_LAYOUT_WORDS: Record<RecordingLayout, string> = {
  mixed: "Both sides",
  agent: "Your side",
  customer: "Their side",
};

export type SetDispositionInput = {
  callId: string;
  disposition: string | null;
  note: string | null;
};

export function useSetCallDisposition(api: ApiClient) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (input: SetDispositionInput) =>
      api.request<CallDetailOut>(`/api/v1/calls/${input.callId}/disposition`, {
        method: "PATCH",
        json: { disposition: input.disposition, note: input.note },
      }),
    onSuccess: (data, variables) => {
      qc.setQueryData(["call", variables.callId], data);
      void qc.invalidateQueries({ queryKey: ["calls"] });
    },
  });
}

export function useCanUseCallNumber(
  api: ApiClient,
  ourE164: string | null,
): { canUse: boolean; isLoading: boolean } {
  const inboxesQuery = useQuery({
    queryKey: ["inboxes"],
    queryFn: () => fetchInboxes(api),
    staleTime: 1000,
  });

  const inboxes = inboxesQuery.data ?? [];
  const owningInbox = inboxes.find((inbox) => inbox.e164 === ourE164) ?? null;
  const isLoading = inboxesQuery.isLoading;

  let canUse = false;
  if (inboxesQuery.isError) {
    canUse = false;
  } else if (owningInbox) {
    canUse = owningInbox.my_role !== "viewer";
  } else {
    canUse = !isLoading && inboxes.length === 0;
  }

  return { canUse, isLoading };
}
