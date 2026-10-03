import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useAuth } from "@/auth/AuthContext";
import { useGate } from "@/api/capabilities";
import type { ApiClient } from "@/api/client";
import {
  Button,
  Input,
  MutationStatus,
  Pill,
  Section,
  Select,
  Spinner,
  mutationErrorMessage,
} from "@/components/ui/primitives";

const DIGEST_PATH = "/api/v1/analytics/delivery-digest";
const DIGEST_TEST_PATH = "/api/v1/analytics/delivery-digest/test";

export const DELIVERY_DIGEST_QUERY_KEY = ["delivery-digest"] as const;

const DESCRIPTION =
  "Every morning we email a summary of yesterday's texts: how many were delivered, why any failed, and your busiest numbers. It's only sent on days you sent texts.";

const DEFAULT_TZ = "America/New_York";

const TIME_ZONES: { value: string; label: string }[] = [
  { value: "America/New_York", label: "Eastern" },
  { value: "America/Chicago", label: "Central" },
  { value: "America/Denver", label: "Mountain" },
  { value: "America/Phoenix", label: "Arizona" },
  { value: "America/Los_Angeles", label: "Pacific" },
  { value: "America/Anchorage", label: "Alaska" },
  { value: "Pacific/Honolulu", label: "Hawaii" },
  { value: "Europe/London", label: "UK" },
];

export type DeliveryDigestSettings = {
  enabled: boolean;
  hour: number;
  tz: string;
  recipients: string[];
  default_recipients: string[];
  last_sent_at: string | null;
};

export type DeliveryDigestTestResult = {
  sent: boolean;
  day: string | null;
};

type DeliveryDigestSavePayload = {
  enabled: boolean;
  hour: number;
  tz: string;
  recipients: string[];
};

type DeliveryDigestForm = Omit<DeliveryDigestSavePayload, "recipients"> & {
  recipients: string;
};

/** `enabled` gates the request so a caller without `settings:read` never touches the
 * endpoint; the default keeps the single-argument call site working. */
export function useDeliveryDigest(api: ApiClient, enabled = true) {
  return useQuery({
    queryKey: DELIVERY_DIGEST_QUERY_KEY,
    queryFn: () => api.request<DeliveryDigestSettings>(DIGEST_PATH),
    enabled,
  });
}

export function useSaveDeliveryDigest(api: ApiClient) {
  const queryClient = useQueryClient();
  return useMutation({
    mutationFn: (payload: DeliveryDigestSavePayload) =>
      api.request<DeliveryDigestSettings>(DIGEST_PATH, {
        method: "PUT",
        json: payload,
      }),
    onSuccess: (data) => {
      queryClient.setQueryData(DELIVERY_DIGEST_QUERY_KEY, data);
    },
  });
}

export function useSendDeliveryDigestTest(api: ApiClient) {
  return useMutation({
    mutationFn: () =>
      api.request<DeliveryDigestTestResult>(DIGEST_TEST_PATH, {
        method: "POST",
      }),
  });
}

/** "6 AM" / "12 PM" / "8 PM". */
function hourLabel(hour: number): string {
  const suffix = hour < 12 ? "AM" : "PM";
  const display = hour % 12 === 0 ? 12 : hour % 12;
  return `${display} ${suffix}`;
}

/** Comma, whitespace and semicolon separated; trimmed; empties dropped. */
export function parseRecipients(text: string): string[] {
  return text
    .split(/[\s,;]+/)
    .map((value) => value.trim())
    .filter((value) => value.length > 0);
}

export function DeliveryDigestCard() {
  const { api } = useAuth();
  const gate = useGate();
  const canRead = gate.can("settings:read");
  const canWrite = gate.can("settings:write");

  const digestQuery = useDeliveryDigest(api, canRead);
  const save = useSaveDeliveryDigest(api);
  const sendTest = useSendDeliveryDigestTest(api);

  const data = digestQuery.data;

  /** Unedited form falls back to the loaded settings, so the fields never flash defaults
   * after the query resolves; the first edit freezes a draft and marks the form dirty. */
  const [draft, setDraft] = React.useState<DeliveryDigestForm | null>(null);

  const seed = React.useMemo<DeliveryDigestForm>(
    () => ({
      enabled: data?.enabled ?? false,
      hour: data?.hour ?? 6,
      tz: data?.tz ?? DEFAULT_TZ,
      recipients: data?.recipients.join(", ") ?? "",
    }),
    [data],
  );

  const form = draft ?? seed;
  const dirty = draft != null;

  const tzOptions = React.useMemo(() => {
    if (form.tz && !TIME_ZONES.some((zone) => zone.value === form.tz)) {
      return [...TIME_ZONES, { value: form.tz, label: "" }];
    }
    return TIME_ZONES;
  }, [form.tz]);

  if (!canRead) return null;

  if (digestQuery.isPending) {
    return (
      <Section title="Daily delivery email" description={DESCRIPTION}>
        <Spinner label="Loading daily delivery email settings" />
      </Section>
    );
  }

  if (digestQuery.isError) {
    return (
      <Section title="Daily delivery email" description={DESCRIPTION}>
        <div className="space-y-2">
          <p role="alert" className="text-sm text-destructive">
            {mutationErrorMessage(digestQuery.error)}
          </p>
          <Button
            type="button"
            size="sm"
            variant="outline"
            onClick={() => {
              void digestQuery.refetch();
            }}
          >
            Retry
          </Button>
        </div>
      </Section>
    );
  }

  if (!data) return null;

  const defaultText = data.default_recipients.join(", ");

  const inputsDisabled = !canWrite || save.isPending;
  const saveDisabled = inputsDisabled || !dirty;
  const testDisabled = !canWrite || sendTest.isPending;
  const testResult = sendTest.data;

  const onChange = (patch: Partial<DeliveryDigestForm>) => {
    setDraft((current) => ({ ...(current ?? seed), ...patch }));
  };

  const onSave = () => {
    save.mutate(
      {
        enabled: form.enabled,
        hour: form.hour,
        tz: form.tz,
        recipients: parseRecipients(form.recipients),
      },
      { onSuccess: () => setDraft(null) },
    );
  };

  return (
    <Section
      title="Daily delivery email"
      description={DESCRIPTION}
      actions={
        form.enabled ? <Pill tone="success">On</Pill> : <Pill tone="neutral">Off</Pill>
      }
    >
      {!canWrite ? (
        <p className="text-xs text-muted-foreground">
          You can view this, but only an admin can make changes here.
        </p>
      ) : null}

      <MutationStatus
        pending={save.isPending}
        error={save.error}
        success={save.isSuccess ? "Saved." : undefined}
      />

      <fieldset disabled={!canWrite} className="m-0 min-w-0 border-0 p-0">
        <div className="space-y-4">
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={form.enabled}
              onChange={(event) => onChange({ enabled: event.target.checked })}
              disabled={inputsDisabled}
            />
            <span className="font-medium">Send the daily delivery email</span>
          </label>

          <label className="block space-y-1">
            <span className="text-sm text-muted-foreground">Send at</span>
            <Select
              aria-label="Send at"
              value={form.hour}
              onChange={(event) => onChange({ hour: Number(event.target.value) })}
              disabled={inputsDisabled}
            >
              {Array.from({ length: 24 }, (_, value) => (
                <option key={value} value={value}>
                  {hourLabel(value)}
                </option>
              ))}
            </Select>
          </label>

          <label className="block space-y-1">
            <span className="text-sm text-muted-foreground">Time zone</span>
            <Select
              aria-label="Time zone"
              value={form.tz}
              onChange={(event) => onChange({ tz: event.target.value })}
              disabled={inputsDisabled}
            >
              {tzOptions.map((zone) => (
                <option key={zone.value} value={zone.value}>
                  {zone.label ? `${zone.value} (${zone.label})` : zone.value}
                </option>
              ))}
            </Select>
          </label>

          <label className="block space-y-1">
            <span className="text-sm text-muted-foreground">Send to</span>
            <Input
              aria-label="Send to"
              type="text"
              value={form.recipients}
              onChange={(event) => onChange({ recipients: event.target.value })}
              disabled={inputsDisabled}
              placeholder={defaultText}
            />
            <span className="text-xs text-muted-foreground">
              Leave empty to send to the workspace owners and admins ({defaultText}).
            </span>
          </label>

          <div className="flex flex-wrap items-center gap-2">
            <Button type="button" size="sm" onClick={onSave} disabled={saveDisabled}>
              Save
            </Button>
            <Button
              type="button"
              size="sm"
              variant="outline"
              onClick={() => sendTest.mutate()}
              disabled={testDisabled}
            >
              Send me a test
            </Button>
          </div>

          {testResult ? (
            <p role="status" aria-live="polite" className="text-sm text-muted-foreground">
              {testResult.sent
                ? `Test sent for ${testResult.day}. Check your inbox.`
                : "Nothing to send yet: no texts in the last 7 days."}
            </p>
          ) : null}

          {sendTest.isError ? (
            <p role="alert" className="text-sm text-destructive">
              {mutationErrorMessage(sendTest.error)}
            </p>
          ) : null}

          {data.last_sent_at ? (
            <p className="text-xs text-muted-foreground">
              Last sent {new Date(data.last_sent_at).toLocaleString()}
            </p>
          ) : null}
        </div>
      </fieldset>
    </Section>
  );
}
