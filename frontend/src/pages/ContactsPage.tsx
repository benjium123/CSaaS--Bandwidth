import * as React from "react";
import { useQueryClient } from "@tanstack/react-query";
import { useAuth } from "@/auth/AuthContext";
import { useContacts } from "@/api/hooks";
import { Button, Input, Spinner } from "@/components/ui/primitives";
import { formatPhone } from "@/lib/format";

export function ContactsPage() {
  const { api } = useAuth();
  const qc = useQueryClient();
  const [q, setQ] = React.useState("");
  const { data, isLoading } = useContacts(api, q);
  const [name, setName] = React.useState("");
  const [phone, setPhone] = React.useState("");
  const [error, setError] = React.useState<string | null>(null);

  async function create(e: React.FormEvent) {
    e.preventDefault();
    setError(null);
    try {
      await api.request("/api/v1/contacts", {
        method: "POST",
        json: {
          display_name: name,
          phones: phone ? [{ e164: phone, label: "mobile", is_primary: true }] : [],
        },
      });
      setName("");
      setPhone("");
      qc.invalidateQueries({ queryKey: ["contacts"] });
    } catch (err) {
      setError((err as Error).message);
    }
  }

  return (
    <div className="mx-auto max-w-2xl space-y-4 p-6">
      <h1 className="text-lg font-semibold">Contacts</h1>

      <form className="flex flex-wrap gap-2" onSubmit={create}>
        <Input
          aria-label="Contact name"
          placeholder="Name"
          className="flex-1"
          value={name}
          onChange={(e) => setName(e.target.value)}
        />
        <Input
          aria-label="Contact phone"
          placeholder="+19725550199"
          className="flex-1"
          value={phone}
          onChange={(e) => setPhone(e.target.value)}
        />
        <Button type="submit" disabled={!name.trim()}>
          Add
        </Button>
      </form>
      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}

      <Input
        aria-label="Search contacts"
        placeholder="Search name or number"
        value={q}
        onChange={(e) => setQ(e.target.value)}
      />

      {isLoading ? (
        <Spinner />
      ) : (
        <ul className="divide-y divide-border rounded-md border border-border">
          {(data ?? []).map((c) => (
            <li key={c.id} className="flex justify-between px-3 py-2 text-sm">
              <span>{c.display_name}</span>
              <span className="text-xs text-muted-foreground">
                {c.phones.map((p) => formatPhone(p.e164)).join(", ")}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
