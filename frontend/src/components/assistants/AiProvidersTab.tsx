import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useAuth } from "@/auth/AuthContext";
import {
  AI_KINDS,
  AI_KIND_WORDS,
  AI_PROVIDER_FIELDS,
  AI_PROVIDERS_BY_KIND,
  AI_PROVIDERS_KEY,
  AI_SETTINGS_KEY,
  aiProviderLabel,
  createAiProvider,
  deleteAiProvider,
  fetchAiProviders,
  fetchAiSettings,
  fieldsForAccount,
  missingKindWords,
  patchAiProvider,
  patchAiSettings,
  probeAiProvider,
  type AiKind,
  type AiProviderAccount,
  type AiProviderField,
  type AiProviderStatus,
} from "@/api/assistants";
import {
  Button,
  Card,
  EmptyState,
  Input,
  MutationStatus,
  Pill,
  Section,
  Select,
  Spinner,
  type PillTone,
} from "@/components/ui/primitives";

function capitalizeWord(word: string): string {
  return word.charAt(0).toUpperCase() + word.slice(1);
}

function statusPill(status: AiProviderStatus): { label: string; tone: PillTone } {
  switch (status) {
    case "unverified":
      return { label: "Not checked yet", tone: "neutral" };
    case "active":
      return { label: "Working", tone: "success" };
    case "failed":
      return { label: "Not working", tone: "danger" };
    case "disabled":
      return { label: "Turned off", tone: "neutral" };
    default:
      return { label: "Not checked yet", tone: "neutral" };
  }
}

function buildCredentialsForSave(
  fields: AiProviderField[],
  account: AiProviderAccount | undefined,
  values: Record<string, string>,
): Record<string, string> {
  const next: Record<string, string> = {};
  for (const field of fields) {
    const value = values[field.name] ?? "";
    if (account && value === "") continue;
    next[field.name] = value;
  }
  return next;
}

function Readiness({
  mode,
  byokReady,
  missingKinds,
}: {
  mode: "platform" | "byok" | undefined;
  byokReady: boolean;
  missingKinds: AiKind[];
}) {
  if (mode === "platform") {
    return (
      <div className="space-y-2">
        <Pill tone="info">Using CSaaS keys</Pill>
        <p className="text-sm text-muted-foreground">You pay for what your assistants use.</p>
      </div>
    );
  }

  if (byokReady) {
    return (
      <div className="space-y-2">
        <Pill tone="success">Ready</Pill>
        <p className="text-sm text-muted-foreground">Your assistants can go live.</p>
      </div>
    );
  }

  return (
    <div className="space-y-2">
      <Pill tone="warning">Still needed</Pill>
      {/* An empty list would render "Add a connection for: ." - if the server says not ready
          but names nothing, say the honest general thing instead. */}
      <p className="text-sm text-muted-foreground">
        {missingKinds.length > 0
          ? `Add a connection for: ${missingKindWords(missingKinds)}.`
          : "Connect a language model, speech recognition and a voice before going live."}
      </p>
      {missingKinds.length > 0 && (
        <ul aria-label="What is still needed" className="space-y-1 text-sm text-muted-foreground">
          {missingKinds.map((kind) => (
            <li key={kind}>Add a {AI_KIND_WORDS[kind]} connection.</li>
          ))}
        </ul>
      )}
    </div>
  );
}

function AiProviderAccountCard({ account }: { account: AiProviderAccount }) {
  const { api } = useAuth();
  const queryClient = useQueryClient();
  const fields = fieldsForAccount(account);
  const [label, setLabel] = React.useState(account.label);
  const [credentials, setCredentials] = React.useState<Record<string, string>>(() =>
    Object.fromEntries(fields.map((field) => [field.name, ""])),
  );
  const [confirmingRemove, setConfirmingRemove] = React.useState(false);
  const [lastAction, setLastAction] = React.useState<
    "save" | "probe" | "disable" | "remove" | null
  >(null);

  function invalidateProviderQueries() {
    void queryClient.invalidateQueries({ queryKey: AI_PROVIDERS_KEY });
    void queryClient.invalidateQueries({ queryKey: AI_SETTINGS_KEY });
  }

  function clearSecretInputs() {
    setCredentials((prev) => {
      const next = { ...prev };
      for (const field of fields) {
        if (field.secret) next[field.name] = "";
      }
      return next;
    });
  }

  const saveMutation = useMutation({
    mutationFn: async () => {
      const patch: { label?: string; credentials?: Record<string, string> } = {};
      // Blank means "keep what is stored", so an all-blank form must send NO credentials
      // key at all - sending `{}` invites the backend to treat it as "clear them".
      const next = buildCredentialsForSave(fields, account, credentials);
      if (Object.keys(next).length > 0) patch.credentials = next;
      if (label.trim() !== "" && label.trim() !== account.label) {
        patch.label = label.trim();
      }
      return patchAiProvider(api, account.id, patch);
    },
    onSuccess: (saved) => {
      queryClient.setQueryData<AiProviderAccount[]>(AI_PROVIDERS_KEY, (old) =>
        (old ?? []).map((item) => (item.id === saved.id ? saved : item)),
      );
      clearSecretInputs();
      setLabel(saved.label);
      invalidateProviderQueries();
    },
  });

  const probeMutation = useMutation({
    mutationFn: () => probeAiProvider(api, account.id),
    onSuccess: () => invalidateProviderQueries(),
  });

  const toggleMutation = useMutation({
    mutationFn: () => {
      const nextStatus: "disabled" | "unverified" =
        account.status === "disabled" ? "unverified" : "disabled";
      return patchAiProvider(api, account.id, { status: nextStatus });
    },
    onSuccess: () => invalidateProviderQueries(),
  });

  const removeMutation = useMutation({
    mutationFn: () => deleteAiProvider(api, account.id),
    onSuccess: () => {
      setConfirmingRemove(false);
      invalidateProviderQueries();
    },
  });

  const pill = statusPill(account.status);

  return (
    <Card role="group" aria-label={`${account.label} connection`} className="space-y-4">
      <div className="flex flex-wrap items-start justify-between gap-3">
        <div className="space-y-1">
          <div className="flex flex-wrap items-center gap-2">
            <h3 className="text-sm font-semibold text-foreground">{account.label}</h3>
            <Pill tone={pill.tone}>{pill.label}</Pill>
          </div>
          <p className="text-xs text-muted-foreground">
            {aiProviderLabel(account.provider)} · {AI_KIND_WORDS[account.kind]}
          </p>
          {account.last_probe_detail && (
            <p className="text-xs text-muted-foreground">{account.last_probe_detail}</p>
          )}
        </div>
        <div className="flex items-center gap-2">
          <Button
            type="button"
            variant="outline"
            size="sm"
            aria-label={`Test ${account.label}`}
            disabled={probeMutation.isPending}
            onClick={() => {
              setLastAction("probe");
              probeMutation.mutate();
            }}
          >
            Test
          </Button>
          <Button
            type="button"
            variant="outline"
            size="sm"
            aria-label={
              account.status === "disabled"
                ? `Turn on ${account.label}`
                : `Disable ${account.label}`
            }
            disabled={toggleMutation.isPending}
            onClick={() => {
              setLastAction("disable");
              toggleMutation.mutate();
            }}
          >
            {account.status === "disabled" ? "Turn on" : "Disable"}
          </Button>
          <Button
            type="button"
            variant="outline"
            size="sm"
            aria-label={
              confirmingRemove ? `Confirm remove ${account.label}` : `Remove ${account.label}`
            }
            disabled={removeMutation.isPending}
            onClick={() => {
              if (confirmingRemove) {
                setLastAction("remove");
                removeMutation.mutate();
              } else {
                setConfirmingRemove(true);
              }
            }}
          >
            {confirmingRemove ? `Confirm remove ${account.label}` : "Remove"}
          </Button>
        </div>
      </div>

      <form
        onSubmit={(event) => {
          event.preventDefault();
          setLastAction("save");
          saveMutation.mutate();
        }}
        className="grid gap-3 md:grid-cols-2"
      >
        <div className="space-y-1">
          <label htmlFor={`${account.id}-label`} className="block text-xs text-muted-foreground">
            Label
          </label>
          <Input
            id={`${account.id}-label`}
            value={label}
            onChange={(event) => setLabel(event.target.value)}
          />
        </div>
        {fields.map((field) => (
          <div key={field.name} className="space-y-1">
            <label htmlFor={`${account.id}-${field.name}`} className="block text-xs text-muted-foreground">
              {field.label}
            </label>
            {/* F2: the value comes from local state for EVERY field, secret ones included.
                Secret state starts empty and is cleared again after a save, so nothing
                stored is ever echoed - but pinning a secret input to "" would have made it
                impossible to type a replacement key at all. */}
            <Input
              id={`${account.id}-${field.name}`}
              type={field.secret ? "password" : "text"}
              value={credentials[field.name] ?? ""}
              onChange={(event) =>
                setCredentials((prev) => ({ ...prev, [field.name]: event.target.value }))
              }
              placeholder={field.secret ? "stored — leave blank to keep" : undefined}
            />
            {field.secret && account.has_credentials && (
              <span className="text-xs text-muted-foreground">Key saved</span>
            )}
          </div>
        ))}
        <div className="md:col-span-2 flex flex-wrap items-center gap-3">
          <Button type="submit" size="sm" disabled={saveMutation.isPending}>
            Save
          </Button>
          {lastAction === "save" && (
            <MutationStatus
              pending={saveMutation.isPending}
              error={saveMutation.error}
              success="Saved"
              pendingLabel="Saving…"
            />
          )}
          {lastAction === "probe" && (
            <MutationStatus
              pending={probeMutation.isPending}
              error={probeMutation.error}
              success="Tested"
              pendingLabel="Testing…"
            />
          )}
          {lastAction === "disable" && (
            <MutationStatus
              pending={toggleMutation.isPending}
              error={toggleMutation.error}
              success={account.status === "disabled" ? "Turned on" : "Disabled"}
              pendingLabel={account.status === "disabled" ? "Turning on…" : "Disabling…"}
            />
          )}
          {lastAction === "remove" && (
            <MutationStatus
              pending={removeMutation.isPending}
              error={removeMutation.error}
              success="Removed"
              pendingLabel="Removing…"
            />
          )}
        </div>
      </form>
    </Card>
  );
}

function ConnectAiProviderSection() {
  const { api } = useAuth();
  const queryClient = useQueryClient();
  const [kind, setKind] = React.useState<AiKind | "">("");
  const [provider, setProvider] = React.useState("");
  const [label, setLabel] = React.useState("");
  const [credentials, setCredentials] = React.useState<Record<string, string>>({});
  const [createAttempted, setCreateAttempted] = React.useState(false);

  const fields = provider ? AI_PROVIDER_FIELDS[provider] ?? [] : [];

  function handleKindChange(value: string) {
    const next = value as AiKind | "";
    setKind(next);
    setCreateAttempted(false);
    setProvider("");
    setCredentials({});
  }

  function handleProviderChange(value: string) {
    setProvider(value);
    setCreateAttempted(false);
    if (value) {
      const nextFields = AI_PROVIDER_FIELDS[value] ?? [];
      setCredentials(Object.fromEntries(nextFields.map((field) => [field.name, ""])));
      setLabel(aiProviderLabel(value));
    } else {
      setCredentials({});
    }
  }

  const createMutation = useMutation({
    mutationFn: async () => {
      if (!kind || !provider) {
        throw new Error("Choose a purpose and a provider first.");
      }
      return createAiProvider(api, {
        kind,
        provider,
        label: label.trim(),
        credentials: buildCredentialsForSave(fields, undefined, credentials),
      });
    },
    onSuccess: (saved) => {
      queryClient.setQueryData<AiProviderAccount[]>(AI_PROVIDERS_KEY, (old) => [
        ...(old ?? []),
        saved,
      ]);
      // The just-saved connection can flip ai-settings byok_ready / missing_kinds, so
      // refresh the readiness pill now instead of waiting for the user to navigate.
      void queryClient.invalidateQueries({ queryKey: AI_SETTINGS_KEY });

      setKind("");
      setProvider("");
      setLabel("");
      setCredentials({});

      // Fire-and-forget probe: it must not block the form and must not surface as a
      // save error. Refetch after it settles so the new card shows last_probe_detail.
      void probeAiProvider(api, saved.id)
        .catch(() => undefined)
        .finally(() => {
          void queryClient.invalidateQueries({ queryKey: AI_PROVIDERS_KEY });
        });
    },
  });

  const missingFields = [
    ...(kind === "" ? ["Purpose"] : []),
    ...(provider === "" ? ["Provider"] : []),
    ...(label.trim() === "" ? ["Label"] : []),
    ...fields
      .filter((field) => (credentials[field.name] ?? "").trim() === "")
      .map((field) => field.label),
  ];
  const createIncomplete = missingFields.length > 0;

  return (
    <div className="space-y-3">
      {/* Deliberately NOT wrapped in <Section> - a heading reading "Connect a provider"
          would give the landmark the same accessible name as the picker inside it. */}
      <h2 className="text-sm font-semibold text-foreground">Connect a provider</h2>
      <Select
        aria-label="What this connection is for"
        value={kind}
        onChange={(event) => handleKindChange(event.target.value)}
      >
        <option value="">Choose…</option>
        {AI_KINDS.map((item) => (
          <option key={item} value={item}>
            {capitalizeWord(AI_KIND_WORDS[item])}
          </option>
        ))}
      </Select>

      {kind !== "" && (
        <Select
          aria-label="Connect a provider"
          value={provider}
          onChange={(event) => handleProviderChange(event.target.value)}
        >
          <option value="">Choose a provider…</option>
          {AI_PROVIDERS_BY_KIND[kind].map((name) => (
            <option key={name} value={name}>
              {aiProviderLabel(name)}
            </option>
          ))}
        </Select>
      )}

      {provider !== "" && (
        <Card className="space-y-3">
          <form
            onSubmit={(event) => {
              event.preventDefault();
              setCreateAttempted(true);
              createMutation.mutate();
            }}
            className="grid gap-3 md:grid-cols-2"
          >
            <div className="space-y-1">
              <label htmlFor="connect-ai-label" className="block text-xs text-muted-foreground">
                Name this connection
              </label>
              <Input
                id="connect-ai-label"
                value={label}
                onChange={(event) => setLabel(event.target.value)}
                aria-invalid={label.trim() === ""}
              />
            </div>
            {fields.map((field) => (
              <div key={field.name} className="space-y-1">
                <label htmlFor={`connect-ai-${field.name}`} className="block text-xs text-muted-foreground">
                  {field.label}
                </label>
                <Input
                  id={`connect-ai-${field.name}`}
                  type={field.secret ? "password" : "text"}
                  value={credentials[field.name] ?? ""}
                  onChange={(event) =>
                    setCredentials((prev) => ({ ...prev, [field.name]: event.target.value }))
                  }
                  aria-invalid={(credentials[field.name] ?? "").trim() === ""}
                />
              </div>
            ))}
            {createIncomplete && (
              <p className="md:col-span-2 text-xs text-destructive">
                Missing: {missingFields.join(", ")}
              </p>
            )}
            <div className="md:col-span-2 flex flex-wrap items-center gap-3">
              <Button type="submit" size="sm" disabled={createMutation.isPending || createIncomplete}>
                Save
              </Button>
              {/* Same convention as SmartRoutingControl: MutationStatus renders "Saved"
                  unconditionally once mounted, so keep it unmounted until a save was
                  actually triggered. */}
              {createAttempted && (
                <MutationStatus
                  pending={createMutation.isPending}
                  error={createMutation.error}
                  success="Saved"
                  pendingLabel="Saving…"
                />
              )}
            </div>
          </form>
        </Card>
      )}
    </div>
  );
}

export function AiProvidersTab() {
  const { api } = useAuth();
  const queryClient = useQueryClient();

  const settingsQuery = useQuery({
    queryKey: AI_SETTINGS_KEY,
    queryFn: () => fetchAiSettings(api),
  });
  const providersQuery = useQuery({
    queryKey: AI_PROVIDERS_KEY,
    queryFn: () => fetchAiProviders(api),
  });

  const saveSettingsMutation = useMutation({
    mutationFn: (mode: "platform" | "byok") => patchAiSettings(api, { ai_key_mode: mode }),
    onSuccess: (saved) => {
      queryClient.setQueryData(AI_SETTINGS_KEY, saved);
    },
  });

  const currentMode = settingsQuery.data?.ai_key_mode;
  const missingKinds = settingsQuery.data?.missing_kinds ?? [];
  const byokReady = settingsQuery.data?.byok_ready ?? false;
  const providers = providersQuery.data ?? [];

  function handleModeClick(mode: "platform" | "byok") {
    if (currentMode === mode) return;
    saveSettingsMutation.mutate(mode);
  }

  return (
    <div className="mx-auto max-w-5xl space-y-8 p-6 text-foreground">
      <div className="space-y-1">
        <h1 className="text-lg font-semibold">AI providers</h1>
        <p className="text-sm text-muted-foreground">
          Choose whose keys your assistants use, and connect the ones you bring yourself.
        </p>
      </div>

      <Card className="space-y-4">
        <div className="flex flex-wrap items-center gap-3">
          <div
            role="radiogroup"
            aria-label="Whose keys your assistants use"
            className="flex flex-wrap items-center gap-2"
          >
            <Button
              type="button"
              role="radio"
              aria-checked={currentMode === "platform"}
              variant={currentMode === "platform" ? "default" : "outline"}
              onClick={() => handleModeClick("platform")}
            >
              Use CSaaS keys (billed per use)
            </Button>
            <Button
              type="button"
              role="radio"
              aria-checked={currentMode === "byok"}
              variant={currentMode === "byok" ? "default" : "outline"}
              onClick={() => handleModeClick("byok")}
            >
              Use my own keys
            </Button>
          </div>
          {/* Item 45 convention: MutationStatus renders "Saved" unconditionally once
              mounted, so it must stay unmounted until a save was actually triggered. */}
          {saveSettingsMutation.status !== "idle" && (
            <MutationStatus
              pending={saveSettingsMutation.isPending}
              error={saveSettingsMutation.error}
              success="Saved"
              pendingLabel="Saving…"
            />
          )}
        </div>

        {settingsQuery.isLoading ? (
          <Spinner label="Loading settings" />
        ) : settingsQuery.isError ? (
          <div role="alert" className="space-y-2 text-sm text-destructive">
            <p>Settings are unavailable.</p>
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => void settingsQuery.refetch()}
            >
              Retry
            </Button>
          </div>
        ) : (
          <Readiness mode={currentMode} byokReady={byokReady} missingKinds={missingKinds} />
        )}
      </Card>

      <ConnectAiProviderSection />

      <Section title="Connected providers">
        {providersQuery.isLoading ? (
          <p className="text-sm text-muted-foreground">Loading connections…</p>
        ) : providersQuery.isError ? (
          <div role="alert" className="space-y-2 text-sm text-destructive">
            <p>Connections are unavailable.</p>
            <Button
              type="button"
              variant="outline"
              size="sm"
              onClick={() => void providersQuery.refetch()}
            >
              Retry
            </Button>
          </div>
        ) : providers.length === 0 ? (
          /* No CTA: the picker is the section directly above this one, so a button
             repeating that exact phrase adds nothing and makes every getByRole("button",
             { name: "Connect a provider" }) ambiguous. */
          <EmptyState
            title="No AI connections yet"
            description="Use the picker above to add your first one."
          />
        ) : (
          providers.map((account) => <AiProviderAccountCard key={account.id} account={account} />)
        )}
      </Section>
    </div>
  );
}
