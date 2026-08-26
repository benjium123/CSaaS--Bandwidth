import * as React from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useAuth } from "@/auth/AuthContext";
import { useNumbers } from "@/api/hooks";
import { Button, Input, Spinner } from "@/components/ui/primitives";
import { formatPhone } from "@/lib/format";

export function NumbersPage() {
  const { api } = useAuth();
  const qc = useQueryClient();
  const { data, isLoading } = useNumbers(api);
  const [value, setValue] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);

  async function add(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      await api.request("/api/v1/numbers", { method: "POST", json: { e164: value } });
      setValue("");
      qc.invalidateQueries({ queryKey: ["numbers"] });
    } catch (err) {
      setError((err as Error).message);
    }
  }

  return (
    <div className="mx-auto max-w-xl space-y-4 p-6">
      <h1 className="text-lg font-semibold">Numbers</h1>
      <p className="text-sm text-muted-foreground">
        Numbers are entered by hand in this phase. Search, ordering and porting arrive with
        the numbers module.
      </p>
      <form className="flex gap-2" onSubmit={add}>
        <Input
          aria-label="Phone number"
          placeholder="+12145550100"
          value={value}
          onChange={(e) => setValue(e.target.value)}
        />
        <Button type="submit">Add</Button>
      </form>
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}
      {isLoading ? (
        <Spinner />
      ) : (
        <ul className="divide-y divide-border rounded-md border border-border">
          {(data ?? []).map((n) => (
            <li key={n.id} className="flex justify-between px-3 py-2 text-sm">
              <span>{formatPhone(n.e164)}</span>
              <span className="text-xs text-muted-foreground">
                {n.is_active ? "active" : "inactive"}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
