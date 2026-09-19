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
import { Badge, Button, Spinner, pillToneClass } from "@/components/ui/primitives";
import {
  ConsoleCard,
  ConsoleEmpty,
  FilterPill,
  PageHeader,
  SectionLabel,
} from "@/components/ui/consoleChrome";
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

/** Both list-status families, converted together onto the shared `Pill` tones - they were
 * LIGHT-mode Tailwind chips rendering inside a dark console. ready/accepted -> --cx-live,
 * failed/invalid/dnc -> --cx-danger, duplicate -> muted, everything still in flight ->
 * --cx-flag (yellow, not amber-orange).
 *
 * THE TONE MAPPING IS DELIBERATELY UNTOUCHED by the console-chrome sweep: the sweep is
 * presentation only, and which status counts as a failure is a product decision that was
 * already made here. */
function listStatusBadgeClass(status: string): string {
  switch (status) {
    case "ready":
      return pillToneClass("success");
    case "failed":
      return pillToneClass("danger");
    default:
      return pillToneClass("warning");
  }
}

function rowStatusBadgeClass(status: string): string {
  switch (status) {
    case "accepted":
      return pillToneClass("success");
    case "invalid":
    case "dnc":
      return pillToneClass("danger");
    case "duplicate":
      return pillToneClass("neutral");
    default:
      return pillToneClass("warning");
  }
}

/** One cell of the import report's count strip. `dl` pairs stay `dt`/`dd`. */
function CountCell({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="rounded-[12px] border border-[hsl(var(--cx-line))] bg-[hsl(var(--cx-overlay))] px-[12px] py-[10px]">
      <dt className="text-[11.5px] font-medium text-[hsl(var(--cx-muted))]">{label}</dt>
      <dd className="mt-[3px] text-[15px] font-semibold text-[hsl(var(--cx-text))]">{value}</dd>
    </div>
  );
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
    <div className="grid h-full grid-cols-[minmax(300px,380px)_1fr] bg-[hsl(var(--cx-base))]">
      <aside className="flex min-h-0 flex-col border-r border-[hsl(var(--cx-line))]">
        <div className="space-y-[12px] border-b border-[hsl(var(--cx-line))] p-[14px]">
          <PageHeader title="Lists" description="Import a file, map its columns, read the report." />
          <div className="space-y-[6px] rounded-[12px] border border-dashed border-[hsl(var(--cx-line))] bg-[hsl(var(--cx-overlay))] px-[12px] py-[11px]">
            <input
              ref={fileInputRef}
              aria-label="Upload list file"
              type="file"
              accept=".csv,.xlsx"
              onChange={onFileChosen}
              disabled={uploadList.isPending}
              className="block w-full text-[11.5px] text-[hsl(var(--cx-subtle))] file:mr-3 file:rounded-full file:border-0 file:bg-[hsl(var(--cx-accent))] file:px-[14px] file:py-[6px] file:text-[11.5px] file:font-semibold file:text-[hsl(var(--cx-on-acc))]"
            />
            <p className="text-[11px] text-[hsl(var(--cx-muted))]">CSV or XLSX, with headers.</p>
          </div>
          {uploadList.isPending && <Spinner label="Uploading" />}
          {uploadError && (
            <p role="alert" className="text-[12.5px] text-[hsl(var(--cx-danger))]">
              {uploadError}
            </p>
          )}
        </div>

        <div className="min-h-0 flex-1 overflow-y-auto p-[10px]">
          {isLoading ? (
            <Spinner label="Loading lists" />
          ) : error ? (
            <p role="alert" className="p-[14px] text-[12.5px] text-[hsl(var(--cx-danger))]">
              {(error as Error).message}
            </p>
          ) : (lists ?? []).length === 0 ? (
            <ConsoleEmpty>No lists yet.</ConsoleEmpty>
          ) : (
            <ul aria-label="Contact lists" className="space-y-[6px]">
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
                      "flex w-full flex-col gap-[6px] rounded-[12px] px-[12px] py-[10px] text-left text-[13px] transition-colors",
                      "hover:bg-[hsl(var(--cx-overlay))]",
                      l.id === selectedId && !preview
                        ? "bg-[hsl(var(--cx-lift))] text-[hsl(var(--cx-text))]"
                        : "text-[hsl(var(--cx-subtle))]",
                    )}
                  >
                    <span className="flex items-center justify-between gap-[11px]">
                      <span className="truncate font-medium text-[hsl(var(--cx-text))]">
                        {l.name}
                      </span>
                      <Badge className={listStatusBadgeClass(l.status)}>{l.status}</Badge>
                    </span>
                    <span className="text-[11.5px] text-[hsl(var(--cx-muted))]">
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
          <div className="p-6">
            <ConsoleEmpty>Upload a list or select one to see its import report.</ConsoleEmpty>
          </div>
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
    <div className="space-y-[18px] p-6">
      <PageHeader
        title={`Map columns: ${preview.name}`}
        description={`${preview.row_count} rows detected.`}
        headingLevel={2}
      />

      <div className="space-y-[11px]">
        <SectionLabel>File preview</SectionLabel>
        <div className="overflow-x-auto rounded-[14px] border border-[hsl(var(--cx-line))] bg-[hsl(var(--cx-surface))]">
          <table className="w-full text-[12px]">
            <thead>
              <tr className="border-b border-[hsl(var(--cx-line))] bg-[hsl(var(--cx-overlay))] text-left">
                {preview.headers.map((h) => (
                  <th
                    key={h}
                    className="whitespace-nowrap px-[12px] py-[9px] font-semibold text-[hsl(var(--cx-subtle))]"
                  >
                    {h}
                  </th>
                ))}
              </tr>
            </thead>
            <tbody className="divide-y divide-[hsl(var(--cx-line))]">
              {preview.preview_rows.map((row, i) => (
                <tr key={i}>
                  {preview.headers.map((h) => (
                    <td
                      key={h}
                      className="whitespace-nowrap px-[12px] py-[9px] text-[hsl(var(--cx-muted))]"
                    >
                      {row[h] ?? ""}
                    </td>
                  ))}
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <div className="space-y-[11px]">
        <SectionLabel>Column mapping</SectionLabel>
        <div className="grid max-w-lg grid-cols-2 gap-[12px]">
          {CANONICAL_FIELDS.map((field) => (
            <div key={field.key} className="space-y-[6px]">
              <label
                className="block text-[11.5px] font-medium text-[hsl(var(--cx-muted))]"
                htmlFor={`mapping-${field.key}`}
              >
                {field.label}
                {field.required && " *"}
              </label>
              <select
                id={`mapping-${field.key}`}
                aria-label={`Map ${field.label}`}
                className="h-9 w-full rounded-[10px] border border-[hsl(var(--cx-line))] bg-[hsl(var(--cx-overlay))] px-[10px] text-[13px] text-[hsl(var(--cx-text))] focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-[hsl(var(--cx-accent))]"
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
      </div>

      {error && (
        <p role="alert" className="text-[12.5px] text-[hsl(var(--cx-danger))]">
          {error}
        </p>
      )}

      <div className="flex gap-[11px]">
        <Button
          type="button"
          className="rounded-full"
          onClick={commit}
          disabled={!mapping.phone || commitList.isPending}
        >
          {commitList.isPending ? "Importing…" : "Commit import"}
        </Button>
        <Button type="button" variant="outline" className="rounded-full" onClick={onDiscard}>
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
    <div className="space-y-[18px] p-6">
      <ConsoleCard className="space-y-[12px] p-[18px]">
        <PageHeader
          title={list.name}
          description={list.source_filename}
          headingLevel={2}
          actions={<Badge className={listStatusBadgeClass(list.status)}>{list.status}</Badge>}
        />
        {(list.status === "importing" || (list.status === "failed" && list.error)) && (
          <div className="flex items-center gap-[11px]">
            {list.status === "importing" && (
              <span className="text-[12px] text-[hsl(var(--cx-muted))]">Import in progress…</span>
            )}
            {list.status === "failed" && list.error && (
              <span role="alert" className="text-[12px] text-[hsl(var(--cx-danger))]">
                {list.error}
              </span>
            )}
          </div>
        )}
        <dl className="grid grid-cols-2 gap-[11px] sm:grid-cols-5">
          <CountCell label="Total" value={list.total_rows} />
          <CountCell label="Accepted" value={list.accepted_count} />
          <CountCell label="Invalid" value={list.invalid_count} />
          <CountCell label="Duplicate" value={list.duplicate_count} />
          <CountCell label="DNC" value={list.dnc_count} />
        </dl>
      </ConsoleCard>

      <div className="space-y-[11px]">
        <SectionLabel>Import outcomes</SectionLabel>
        <div className="flex flex-wrap gap-[8px]" role="group" aria-label="Filter rows by status">
          {ROW_STATUS_FILTERS.map((f) => (
            <FilterPill
              key={f.key}
              active={status === f.key}
              // CSS is off in the suites and the only signal that a chip is the live
              // filter was its fill, which announces nothing. `aria-pressed` gives the
              // same state a name.
              aria-pressed={status === f.key}
              onClick={() => setStatus(f.key)}
            >
              {f.label}
            </FilterPill>
          ))}
        </div>

        {rowsLoading ? (
          <Spinner label="Loading rows" />
        ) : (rows ?? []).length === 0 ? (
          <ConsoleEmpty>No rows for this filter.</ConsoleEmpty>
        ) : (
          <div className="overflow-x-auto rounded-[14px] border border-[hsl(var(--cx-line))] bg-[hsl(var(--cx-surface))]">
            <table className="w-full text-[13px]" aria-label="Import outcomes">
              <thead>
                <tr className="border-b border-[hsl(var(--cx-line))] bg-[hsl(var(--cx-overlay))] text-left text-[11.5px] text-[hsl(var(--cx-muted))]">
                  <th className="px-[12px] py-[9px] font-semibold">Row</th>
                  <th className="px-[12px] py-[9px] font-semibold">Phone</th>
                  <th className="px-[12px] py-[9px] font-semibold">Status</th>
                  <th className="px-[12px] py-[9px] font-semibold">Reason</th>
                </tr>
              </thead>
              <tbody className="divide-y divide-[hsl(var(--cx-line))]">
                {(rows as ListRowOut[]).map((r) => (
                  <tr key={r.id}>
                    <td className="px-[12px] py-[10px] text-[12px] text-[hsl(var(--cx-muted))]">
                      {r.row_number}
                    </td>
                    <td className="px-[12px] py-[10px] text-[hsl(var(--cx-text))]">
                      {r.e164 ?? "—"}
                    </td>
                    <td className="px-[12px] py-[10px]">
                      <Badge className={rowStatusBadgeClass(r.status)}>{r.status}</Badge>
                    </td>
                    <td className="px-[12px] py-[10px] text-[12px] text-[hsl(var(--cx-muted))]">
                      {r.reason ?? "—"}
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
    </div>
  );
}
