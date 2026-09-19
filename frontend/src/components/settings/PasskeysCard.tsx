import * as React from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useAuth } from "@/auth/AuthContext";
import { Button, Input, Spinner } from "@/components/ui/primitives";
import { createPasskey, passkeysSupported } from "@/lib/webauthn";

type Passkey = { id: string; name: string; created_at: string; last_used_at: string | null };

const KEY = ["auth", "passkeys"];

/** P41: list, add and remove passkeys. Used on Settings > Security and on the
 * mandatory "secure your account" screen. */
export function PasskeysCard({ onAdded }: { onAdded?: () => void }) {
  const { api, refreshMe } = useAuth();
  const queryClient = useQueryClient();
  const [name, setName] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);

  const list = useQuery({
    queryKey: KEY,
    queryFn: () => api.request<Passkey[]>("/api/v1/auth/passkeys"),
  });

  const add = useMutation({
    mutationFn: async () => {
      const opts = await api.request<{ challenge_id: string; options: unknown }>(
        "/api/v1/auth/passkeys/register/options",
        { method: "POST" },
      );
      const credential = await createPasskey(opts.options);
      return api.request<Passkey>("/api/v1/auth/passkeys/register", {
        method: "POST",
        json: { challenge_id: opts.challenge_id, credential, name: name.trim() || "Passkey" },
      });
    },
    onSuccess: async () => {
      setName("");
      setError(null);
      await queryClient.invalidateQueries({ queryKey: KEY });
      await refreshMe();
      onAdded?.();
    },
    onError: (err) => setError((err as Error).message),
  });

  const remove = useMutation({
    mutationFn: (id: string) =>
      api.request(`/api/v1/auth/passkeys/${id}`, { method: "DELETE" }),
    onSuccess: async () => {
      setError(null);
      await queryClient.invalidateQueries({ queryKey: KEY });
      await refreshMe();
    },
    onError: (err) => setError((err as Error).message),
  });

  const supported = passkeysSupported();

  return (
    <div className="space-y-3">
      <div>
        <p className="text-sm font-medium">Passkeys</p>
        <p className="text-xs text-muted-foreground">
          Sign in with your fingerprint, face or device PIN. Nothing to type, and it cannot be
          phished.
        </p>
      </div>

      {list.isPending ? (
        <Spinner label="Loading passkeys" />
      ) : list.data && list.data.length > 0 ? (
        <ul className="space-y-2.5">
          {list.data.map((p) => (
            <li
              key={p.id}
              className="flex items-center justify-between gap-3 rounded-md border border-border px-3.5 py-2.5"
            >
              <div className="min-w-0">
                <p className="truncate text-sm">{p.name}</p>
                <p className="text-xs text-muted-foreground">
                  Added {new Date(p.created_at).toLocaleDateString()}
                  {p.last_used_at
                    ? ` · last used ${new Date(p.last_used_at).toLocaleDateString()}`
                    : ""}
                </p>
              </div>
              <Button
                type="button"
                variant="ghost"
                size="sm"
                aria-label={`Remove ${p.name}`}
                disabled={remove.isPending}
                onClick={() => remove.mutate(p.id)}
              >
                Remove
              </Button>
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-sm text-muted-foreground">No passkeys yet.</p>
      )}

      {supported ? (
        <div className="flex gap-3">
          <Input
            aria-label="Passkey name"
            placeholder="e.g. Work laptop"
            value={name}
            onChange={(e) => setName(e.target.value)}
          />
          <Button type="button" onClick={() => add.mutate()} disabled={add.isPending}>
            {add.isPending ? "Waiting for device…" : "Add a passkey"}
          </Button>
        </div>
      ) : (
        <p className="text-sm text-muted-foreground">
          This browser does not support passkeys. Use an authenticator app instead.
        </p>
      )}

      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}
    </div>
  );
}
