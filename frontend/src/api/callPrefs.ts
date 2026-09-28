/**
 * Call preferences: do-not-disturb and call forwarding for the signed-in member.
 *
 * The status pill (src/components/shell/StatusPill.tsx) is the only caller. The server may
 * be older than this screen and omit `dnd_until` entirely, so the query normalises the
 * payload rather than trusting it: a missing field must read as "no end", not `undefined`.
 *
 * DND is also derived client-side. A window that has already run out reads as OFF here even
 * before the server notices, because a pill that still says "Do not disturb" after the time
 * is up is the one thing a user notices and a test cannot.
 */
import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useAuth } from "@/auth/AuthContext";

export type Teammate = { user_id: string; name: string };

export type CallPrefs = {
  dnd: boolean;
  dnd_until: string | null;
  forward_to: string | null;
  teammates: Teammate[];
};

export const CALL_PREFS_KEY = ["call-prefs"] as const;
export const CALL_PREFS_PATH = "/api/v1/me/call-prefs";

export type CallPrefsSaveInput = {
  dnd: boolean;
  dnd_until?: string | null;
  forward_to?: string | null;
};

const MINUTE_MS = 60_000;
const HOUR_MS = 60 * MINUTE_MS;
const DAY_MS = 24 * HOUR_MS;
const TICK_MS = 30_000;

function normalizePrefs(raw: Partial<CallPrefs> | null | undefined): CallPrefs {
  const teammates = raw?.teammates;
  return {
    dnd: Boolean(raw?.dnd),
    dnd_until: raw?.dnd_until ?? null,
    forward_to: raw?.forward_to ?? null,
    teammates: Array.isArray(teammates) ? teammates : [],
  };
}

function parseEnd(value: string | null | undefined): number | null {
  if (!value) return null;
  const ms = Date.parse(value);
  return Number.isFinite(ms) ? ms : null;
}

/**
 * A short, human label for how much longer DND lasts.
 *   < 60 min   -> "58m left"
 *   < 24 h     -> "2h 5m left" (the minutes half is omitted when it is zero)
 *   otherwise  -> null: a window this long is not worth counting down.
 */
export function dndTimeLeft(dndUntil: string | null, now: Date = new Date()): string | null {
  const end = parseEnd(dndUntil);
  if (end == null) return null;

  const ms = end - now.getTime();
  if (ms <= 0) return null;

  if (ms < HOUR_MS) {
    return `${Math.floor(ms / MINUTE_MS)}m left`;
  }
  if (ms < DAY_MS) {
    const hours = Math.floor(ms / HOUR_MS);
    const minutes = Math.floor((ms % HOUR_MS) / MINUTE_MS);
    return minutes > 0 ? `${hours}h ${minutes}m left` : `${hours}h left`;
  }
  return null;
}

/** The next 08:00 local time strictly after `now` (so 08:00 today yields tomorrow). */
export function nextEightAm(now: Date = new Date()): Date {
  const candidate = new Date(now.getFullYear(), now.getMonth(), now.getDate(), 8, 0, 0, 0);
  if (candidate.getTime() <= now.getTime()) {
    candidate.setDate(candidate.getDate() + 1);
  }
  return candidate;
}

export function useCallPrefs(): {
  prefs: CallPrefs | undefined;
  isLoading: boolean;
  save: (next: CallPrefsSaveInput) => Promise<void>;
  error: string | null;
} {
  const { api, me, orgId } = useAuth();
  const queryClient = useQueryClient();
  const [error, setError] = React.useState<string | null>(null);
  const [tick, setTick] = React.useState(0);

  const query = useQuery({
    queryKey: CALL_PREFS_KEY,
    enabled: Boolean(me && orgId),
    queryFn: async () => {
      const data = await api.request<Partial<CallPrefs>>(CALL_PREFS_PATH, { method: "GET" });
      return normalizePrefs(data);
    },
  });

  // Tick every 30 s while there is an end time in the future, so the "left" label falls and
  // the pill flips to Available the moment the window closes. The interval never runs when
  // there is nothing to count down.
  const endMs = parseEnd(query.data?.dnd_until);
  const shouldTick = endMs != null && endMs > Date.now();

  React.useEffect(() => {
    if (!shouldTick) return undefined;
    const id = window.setInterval(() => setTick((value) => value + 1), TICK_MS);
    return () => window.clearInterval(id);
  }, [shouldTick]);

  const prefs = React.useMemo(() => {
    const data = query.data;
    if (!data) return undefined;
    const end = parseEnd(data.dnd_until);
    const expired = end != null && end <= Date.now();
    return expired ? { ...data, dnd: false } : data;
    // `tick` is a deliberate dependency: it re-derives `expired` on each interval tick.
  }, [query.data, tick]);

  const mutation = useMutation<Partial<CallPrefs>, unknown, CallPrefsSaveInput>({
    mutationFn: async (next) => {
      const current = queryClient.getQueryData<CallPrefs>(CALL_PREFS_KEY);
      const body = {
        dnd: next.dnd,
        dnd_until: next.dnd ? next.dnd_until ?? null : null,
        // An explicit forward_to (including null = voicemail) wins; only an omitted one keeps
        // the current target, so picking "Voicemail" really does stop forwarding.
        forward_to: next.dnd
          ? ("forward_to" in next ? next.forward_to ?? null : current?.forward_to ?? null)
          : null,
      };
      return api.request<Partial<CallPrefs>>(CALL_PREFS_PATH, { method: "PUT", json: body });
    },
    onSuccess: (data) => {
      setError(null);
      queryClient.setQueryData<CallPrefs>(CALL_PREFS_KEY, (prev) => {
        const teammates = data?.teammates ?? prev?.teammates ?? [];
        return normalizePrefs({ ...(prev ?? {}), ...(data ?? {}), teammates });
      });
    },
    onError: (err) => {
      setError(err instanceof Error && err.message ? err.message : "Could not save");
    },
  });

  const save = React.useCallback(
    async (next: CallPrefsSaveInput) => {
      setError(null);
      try {
        await mutation.mutateAsync(next);
      } catch {
        // onError has already recorded the message that the alert renders.
      }
    },
    [mutation],
  );

  return { prefs, isLoading: query.isLoading, save, error };
}
