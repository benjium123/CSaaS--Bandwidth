/**
 * "Export CSV" for the contacts list (Phase 27).
 *
 * The CSV is built in the background against the REQUESTER's contact visibility, so the
 * file is never auto-downloaded: the user asks, we poll, and they click to take it. The
 * download also needs the auth headers, which a plain <a href> could not send.
 */
import * as React from "react";
import { useAuth } from "@/auth/AuthContext";
import { Button } from "@/components/ui/primitives";
import { getErrorMessage } from "@/api/contacts";
import {
  downloadContactExport,
  useExportJob,
  useStartContactExport,
  type ContactExportFilters,
} from "@/api/contactsPro";

export function ExportContactsButton({
  filters,
  viewId,
}: {
  filters: ContactExportFilters;
  /** When a saved view is active the server re-reads its filters; send the id. */
  viewId: string | null;
}) {
  const { api } = useAuth();
  const [jobId, setJobId] = React.useState<string | null>(null);
  const [error, setError] = React.useState<string | null>(null);

  const startMutation = useStartContactExport(api);
  const jobQuery = useExportJob(api, jobId);
  const job = jobQuery.data;

  function handleStart() {
    setError(null);
    startMutation.mutate(
      {
        filters: viewId ? null : filters,
        view_id: viewId,
      },
      {
        onSuccess: (started) => setJobId(started.job_id),
        onError: (err) => setError(getErrorMessage(err)),
      },
    );
  }

  async function handleDownload() {
    if (!jobId) return;

    setError(null);
    try {
      await downloadContactExport(api, jobId);
    } catch (err) {
      setError(getErrorMessage(err));
    }
  }

  function handleClearJob() {
    setJobId(null);
    setError(null);
  }

  const isRunning = startMutation.isPending || jobId !== null;

  return (
    <div className="flex flex-wrap items-center gap-2">
      {job?.status === "done" ? (
        <>
          <p role="status" className="text-sm text-muted-foreground">
            Your export is ready — {job.rows} contacts.
          </p>
          <Button type="button" size="sm" onClick={handleDownload}>
            Download CSV
          </Button>
          <Button type="button" size="sm" variant="ghost" onClick={handleClearJob}>
            Start over
          </Button>
          {error ? (
            <p role="alert" className="text-sm text-destructive">
              {error}
            </p>
          ) : null}
        </>
      ) : job?.status === "failed" ? (
        <>
          <p role="alert" className="text-sm text-destructive">
            {job.error ?? "That export could not be finished."}
          </p>
          <Button type="button" size="sm" variant="ghost" onClick={handleClearJob}>
            Try again
          </Button>
        </>
      ) : (
        <>
          <Button type="button" size="sm" onClick={handleStart} disabled={isRunning}>
            {isRunning ? "Preparing…" : "Export CSV"}
          </Button>
          {error ? (
            <p role="alert" className="text-sm text-destructive">
              {error}
            </p>
          ) : null}
          {jobQuery.isError ? (
            <>
              <p role="alert" className="text-sm text-destructive">
                {getErrorMessage(jobQuery.error)}
              </p>
              <Button type="button" size="sm" variant="ghost" onClick={handleClearJob}>
                Try again
              </Button>
            </>
          ) : null}
        </>
      )}
    </div>
  );
}
