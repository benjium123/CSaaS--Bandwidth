import * as React from "react";
import { useAuth } from "@/auth/AuthContext";
import type { ApiClient } from "@/api/client";
import {
  useCommitList,
  useList,
  useListRows,
  useLists,
  useUploadList,
  type ListOut,
  type ListPreviewOut,
  type ListRowOut,
} from "@/api/hooks";
import { Badge, Button, Spinner } from "@/components/ui/primitives";
import { cn } from "@/lib/utils";

/** Canonical fields the import pipeline understands (plan DR-8/DR-9/DR-14). Mirrors
 * FIELD_SYNONYMS in backend/app/services/list_parsing.py. */
const CANONICAL_FIELDS: { key: string; label: string; required?: boolean }[] = [
  { key: "phone", label: "Phone", required: true },
  { key: "first_name", label: "First name" },
  { key: "last_name", label: "Last name" },
  { key: "email", label: "Email" },
  { key: "company", label: "Company" },
  { key: "message", label: "Message" },
];

const ROW_STATUS_FILTERS = [
  { key: "", label: "All" },
  { key: "accepted", label: "Accepted" },
  { key: "invalid", label: "Invalid" },
  { key: "duplicate", label: "Duplicate" },
  { key: "dnc", label: "DNC" },
];

function listStatusBadgeClass(status: string): string {
  switch (status) {
    case "ready":
      return "bg-green-100 text-green-800";
    case "failed":
      return "bg-red-100 text-red-800";
    default:
      return "bg-amber-100 text-amber-800";
  }
}

function rowStatusBadgeClass(status: string): string {
  switch (status) {
    case "accepted":
      return "bg-green-100 text-green-800";
    case "invalid":
    case "dnc":
      return "bg-red-100 text-red-800";
    case "duplicate":
      return "bg-gray-100 text-gray-600";
    default:
      return "bg-amber-100 text-amber-800";
  }
}

export function ListsPage() {
  const { api } = useAuth();
  const { data: lists, isLoading, error } = useLists(api);
  const [selectedId, setSelectedId] = React.useState<string | null>(null);
  const [preview, setPreview] = React.useState<ListPreviewOut | null>(null);
  const [uploadError, setUploadError] = React.useState<string | null>(null);
  const fileInputRef = React.useRef<HTMLInputElement>(null);
  const uploadList = useUploadList(api);

  async function onFileChosen(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file) return;
    setUploadError(null);
    try {
      const result = await uploadList.mutateAsync({ file });
      setSelectedId(null);
      setPreview(result);
    } catch (err) {
      setUploadError((err as Error).message);
    } finally {
      if (fileInputRef.current) fileInputRef.current.value = "";
    }
  }

  return (
    <div className="grid h-full grid-cols-[minmax(300px,380px)_1fr]">
      <aside className="flex min-h-0 flex-col border-r border-border">
        <div className="space-y-3 border-b border-border p-3">
          <h1 className="text-lg font-semibold">Lists</h1>
          <div className="space-y-1">
            <input
              ref={fileInputRef}
              aria-label="Upload list file"
              type="file"
              accept=".csv,.xlsx"
              onChange={onFileChosen}
              disabled={uploadList.isPending}
              className="block w-full text-xs"
            />
            <p className="text-[11px] text-muted-foreground">CSV or XLSX, with headers.</p>
          </div>
          {uploadList.isPending && <Spinner label="Uploading" />}
          {uploadError && (
            <p role="alert" className="text-sm text-destructive">
              {uploadError}
            </p>
          )}
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto">
          {isLoading ? (
            <Spinner label="Loading lists" />
          ) : error ? (
            <p role="alert" className="p-4 text-sm text-destructive">
              {(error as Error).message}
            </p>
          ) : (lists ?? []).length === 0 ? (
            <p className="p-4 text-sm text-muted-foreground">No lists yet.</p>
          ) : (
            <ul aria-label="Contact lists">
              {(lists ?? []).map((l) => (
                <li key={l.id}>
                  <button
                    type="button"
                    aria-current={l.id === selectedId ? "true" : undefined}
                    onClick={() => {
                      setPreview(null);
                      setSelectedId(l.id);
                    }}
                    className={cn(
                      "flex w-full flex-col gap-1 px-3 py-2 text-left text-sm hover:bg-muted",
                      l.id === selectedId && !preview && "bg-muted",
                    )}
                  >
                    <span className="flex items-center justify-between gap-2">
                      <span className="truncate font-medium">{l.name}</span>
                      <Badge className={listStatusBadgeClass(l.status)}>{l.status}</Badge>
                    </span>
                    <span className="text-xs text-muted-foreground">
                      {l.total_rows} rows · {l.accepted_count} accepted
                    </span>
                  </button>
                </li>
              ))}
            </ul>
          )}
        </div>
      </aside>

      <section className="min-h-0 overflow-y-auto">
        {preview ? (
          <MappingPanel
            api={api}
            preview={preview}
            onCommitted={(committed) => {
              setPreview(null);
              setSelectedId(committed.id);
            }}
            onDiscard={() => setPreview(null)}
          />
        ) : selectedId ? (
          <ListDetail api={api} listId={selectedId} />
        ) : (
          <p className="p-6 text-sm text-muted-foreground">
            Upload a list or select one to see its import report.
          </p>
        )}
      </section>
    </div>
  );
}

function MappingPanel({
  api,
  preview,
  onCommitted,
  onDiscard,
}: {
  api: ApiClient;
  preview: ListPreviewOut;
  onCommitted: (list: ListOut) => void;
  onDiscard: () => void;
}) {
  const [mapping, setMapping] = React.useState<Record<string, string>>(preview.suggested_mapping);
  const [error, setError] = React.useState<string | null>(null);
  const commitList = useCommitList(api);

  async function commit() {
    setError(null);
    try {
      const committed = await commitList.mutateAsync({ listId: preview.list_id, mapping });
      onCommitted(committed);
    } catch (err) {
      setError((err as Error).message);
    }
  }

  return (
    <div className="space-y-6 p-6">
      <div>
        <h2 className="text-base font-semibold">Map columns: {preview.name}</h2>
        <p className="text-xs text-muted-foreground">{preview.row_count} rows detected.</p>
      </div>

      <div className="overflow-x-auto rounded-md border border-border">
        <table className="w-full text-xs">
          <thead>
            <tr className="border-b border-border bg-muted text-left">
              {preview.headers.map((h) => (
                <th key={h} className="whitespace-nowrap px-2 py-1.5 font-medium">
                  {h}
                </th>
              ))}
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {preview.preview_rows.map((row, i) => (
              <tr key={i}>
                {preview.headers.map((h) => (
                  <td key={h} className="whitespace-nowrap px-2 py-1.5 text-muted-foreground">
                    {row[h] ?? ""}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      <div className="grid max-w-lg grid-cols-2 gap-3">
        {CANONICAL_FIELDS.map((field) => (
          <div key={field.key} className="space-y-1">
            <label
              className="block text-xs text-muted-foreground"
              htmlFor={`mapping-${field.key}`}
            >
              {field.label}
              {field.required && " *"}
            </label>
            <select
              id={`mapping-${field.key}`}
              aria-label={`Map ${field.label}`}
              className="h-9 w-full rounded-md border border-border bg-background px-2 text-sm"
              value={mapping[field.key] ?? ""}
              onChange={(e) =>
                setMapping((m) => {
                  const next = { ...m };
                  if (e.target.value) next[field.key] = e.target.value;
                  else delete next[field.key];
                  return next;
                })
              }
            >
              <option value="">Not mapped</option>
              {preview.headers.map((h) => (
                <option key={h} value={h}>
                  {h}
                </option>
              ))}
            </select>
          </div>
        ))}
      </div>

      {error && (
        <p role="alert" className="text-sm text-destructive">
          {error}
        </p>
      )}

      <div className="flex gap-2">
        <Button type="button" onClick={commit} disabled={!mapping.phone || commitList.isPending}>
          {commitList.isPending ? "Importing…" : "Commit import"}
        </Button>
        <Button type="button" variant="outline" onClick={onDiscard}>
          Cancel
        </Button>
      </div>
    </div>
  );
}

function ListDetail({ api, listId }: { api: ApiClient; listId: string }) {
  const { data: list, isLoading } = useList(api, listId);
  const [status, setStatus] = React.useState("");
  const { data: rows, isLoading: rowsLoading } = useListRows(api, listId, status || undefined);

  if (isLoading || !list) return <Spinner label="Loading list" />;

  return (
    <div className="space-y-6 p-6">
      <div>
        <h2 className="text-base font-semibold">{list.name}</h2>
        <p className="text-xs text-muted-foreground">{list.source_filename}</p>
        <div className="mt-2 flex items-center gap-2">
          <Badge className={listStatusBadgeClass(list.status)}>{list.status}</Badge>
          {list.status === "importing" && (
            <span className="text-xs text-muted-foreground">Import in progress…</span>
          )}
          {list.status === "failed" && list.error && (
            <span role="alert" className="text-xs text-destructive">
              {list.error}
            </span>
          )}
        </div>
        <dl className="mt-3 grid grid-cols-5 gap-x-4 gap-y-1 text-sm">
          <dt className="text-xs text-muted-foreground">Total</dt>
          <dt className="text-xs text-muted-foreground">Accepted</dt>
          <dt className="text-xs text-muted-foreground">Invalid</dt>
          <dt className="text-xs text-muted-foreground">Duplicate</dt>
          <dt className="text-xs text-muted-foreground">DNC</dt>
          <dd>{list.total_rows}</dd>
          <dd>{list.accepted_count}</dd>
          <dd>{list.invalid_count}</dd>
          <dd>{list.duplicate_count}</dd>
          <dd>{list.dnc_count}</dd>
        </dl>
      </div>

      <div className="flex gap-1" role="group" aria-label="Filter rows by status">
        {ROW_STATUS_FILTERS.map((f) => (
          <button
            key={f.key}
            type="button"
            onClick={() => setStatus(f.key)}
            className={cn(
              "rounded-md px-3 py-1 text-xs",
              status === f.key ? "bg-muted font-medium" : "text-muted-foreground hover:bg-muted",
            )}
          >
            {f.label}
          </button>
        ))}
      </div>

      {rowsLoading ? (
        <Spinner label="Loading rows" />
      ) : (rows ?? []).length === 0 ? (
        <p className="text-sm text-muted-foreground">No rows for this filter.</p>
      ) : (
        <table className="w-full text-sm" aria-label="Import outcomes">
          <thead>
            <tr className="border-b border-border text-left text-xs text-muted-foreground">
              <th className="px-2 py-1.5 font-medium">Row</th>
              <th className="px-2 py-1.5 font-medium">Phone</th>
              <th className="px-2 py-1.5 font-medium">Status</th>
              <th className="px-2 py-1.5 font-medium">Reason</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-border">
            {(rows as ListRowOut[]).map((r) => (
              <tr key={r.id}>
                <td className="px-2 py-1.5 text-xs text-muted-foreground">{r.row_number}</td>
                <td className="px-2 py-1.5">{r.e164 ?? "—"}</td>
                <td className="px-2 py-1.5">
                  <Badge className={rowStatusBadgeClass(r.status)}>{r.status}</Badge>
                </td>
                <td className="px-2 py-1.5 text-xs text-muted-foreground">{r.reason ?? "—"}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </div>
  );
}
