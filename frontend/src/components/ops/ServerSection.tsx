import * as React from "react";
import { useQuery } from "@tanstack/react-query";

import { useAuth } from "@/auth/AuthContext";
import {
  fetchServerSnapshot,
  serverSnapshotQueryKey,
  type ServerApp,
  type ServerSnapshot,
} from "@/api/opsServer";
import { Spinner, mutationErrorMessage } from "@/components/ui/primitives";
import { ConsoleEmpty, SectionLabel, SurfaceCard } from "@/components/ui/consoleChrome";

/** Whole-host resources (CPU, memory, disk, load, uptime) plus per-app usage for the ops
 * console "Server" page. Host numbers are read from /proc and the disk of "/" inside the api
 * container, which (with no lxcfs) describe the WHOLE physical host; per-app numbers come from
 * a host job's `docker stats` dump. Gauges and sparklines are inline SVG - no chart library. */

type MetricLevel = "ok" | "warn" | "critical";

const LEVEL_COLORS: Record<MetricLevel, string> = {
  ok: "var(--cx-ok, #22c55e)",
  warn: "var(--cx-warn, #f59e0b)",
  critical: "var(--cx-danger, #ef4444)",
};

const HISTORY_LIMIT = 120; // 10 minutes at one point per 5 s.

type History = {
  cpu: number[];
  memory: number[];
  disk: number[];
  load: number[];
};

const EMPTY_HISTORY: History = { cpu: [], memory: [], disk: [], load: [] };

function levelFor(percent: number | null, warn: number, critical: number): MetricLevel {
  if (percent == null || !Number.isFinite(percent)) return "ok";
  if (percent >= critical) return "critical";
  if (percent >= warn) return "warn";
  return "ok";
}

function appendPoint(points: number[], value: number | null): number[] {
  if (value == null || !Number.isFinite(value)) return points;
  const next = [...points, value];
  return next.length > HISTORY_LIMIT ? next.slice(next.length - HISTORY_LIMIT) : next;
}

function formatBytes(bytes: number | null | undefined): string {
  if (bytes == null || !Number.isFinite(bytes)) return "\u2014";
  const units = ["B", "KB", "MB", "GB", "TB", "PB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  if (unit === 0) return `${Math.round(value)} ${units[unit]}`;
  return `${value.toFixed(1)} ${units[unit]}`;
}

function formatPercent(value: number | null | undefined): string {
  if (value == null || !Number.isFinite(value)) return "\u2014";
  return `${value}%`;
}

function formatUptime(seconds: number | null | undefined): string | null {
  if (seconds == null || !Number.isFinite(seconds) || seconds < 0) return null;
  const total = Math.floor(seconds);
  const days = Math.floor(total / 86400);
  const hours = Math.floor((total % 86400) / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const parts: string[] = [];
  if (days > 0) parts.push(`${days}d`);
  if (days > 0 || hours > 0) parts.push(`${hours}h`);
  parts.push(`${minutes}m`);
  return parts.join(" ");
}

function RingGauge({
  percent,
  level,
  label,
}: {
  percent: number;
  level: MetricLevel;
  label: string;
}): JSX.Element {
  const clamped = Math.max(0, Math.min(100, percent));
  const radius = 32;
  const circumference = 2 * Math.PI * radius;
  const filled = (clamped / 100) * circumference;
  return (
    <svg
      role="img"
      aria-label={`${label} ${Math.round(clamped)}%`}
      viewBox="0 0 80 80"
      className="h-20 w-20 shrink-0"
    >
      <circle
        cx="40"
        cy="40"
        r={radius}
        fill="none"
        strokeWidth="8"
        stroke="var(--cx-border, #d4d4d8)"
      />
      <circle
        cx="40"
        cy="40"
        r={radius}
        fill="none"
        strokeWidth="8"
        strokeLinecap="round"
        stroke={LEVEL_COLORS[level]}
        strokeDasharray={`${filled} ${circumference - filled}`}
        transform="rotate(-90 40 40)"
      />
    </svg>
  );
}

function Sparkline({ points, level }: { points: number[]; level: MetricLevel }): JSX.Element {
  const width = 120;
  const height = 28;
  const visible = points.slice(-HISTORY_LIMIT);
  if (visible.length < 2) {
    return <svg viewBox={`0 0 ${width} ${height}`} className="h-7 w-full" aria-hidden="true" />;
  }
  const step = width / (visible.length - 1);
  const coords = visible.map((value, index) => {
    const clamped = Math.max(0, Math.min(100, value));
    const x = index * step;
    const y = height - (clamped / 100) * height;
    return `${x.toFixed(1)},${y.toFixed(1)}`;
  });
  return (
    <svg
      viewBox={`0 0 ${width} ${height}`}
      className="h-7 w-full"
      preserveAspectRatio="none"
      aria-hidden="true"
    >
      <polyline points={coords.join(" ")} fill="none" stroke={LEVEL_COLORS[level]} strokeWidth="2" />
    </svg>
  );
}

function MetricCard({
  metric,
  title,
  percent,
  level,
  detail,
  history,
}: {
  metric: "cpu" | "memory" | "disk" | "load";
  title: string;
  percent: number | null;
  level: MetricLevel;
  detail: string;
  history: number[];
}): JSX.Element {
  return (
    <div
      data-metric={metric}
      data-level={level}
      className="flex items-center gap-3 rounded-[var(--cx-r-md,14px)] border border-border p-3"
    >
      {percent == null ? (
        <div className="flex h-20 w-20 shrink-0 items-center justify-center text-xs text-muted-foreground">
          Not available
        </div>
      ) : (
        <RingGauge percent={percent} level={level} label={title} />
      )}
      <div className="min-w-0 flex-1 space-y-1">
        <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">
          {title}
        </p>
        <p className="text-lg font-semibold">
          {percent == null ? "\u2014" : `${Math.round(percent)}%`}
        </p>
        {detail ? <p className="truncate text-xs text-muted-foreground">{detail}</p> : null}
        <Sparkline points={history} level={level} />
      </div>
    </div>
  );
}

function ServerBody({
  snapshot,
  history,
  warn,
  critical,
}: {
  snapshot: ServerSnapshot;
  history: History;
  warn: number;
  critical: number;
}): JSX.Element {
  const memory = snapshot.memory;
  const disk = snapshot.disk;
  const load = snapshot.load;
  const apps = snapshot.apps;

  const sampledMs = Date.parse(snapshot.sampled_at);
  const secondsAgo = Number.isFinite(sampledMs)
    ? Math.max(0, Math.round((Date.now() - sampledMs) / 1000))
    : 0;
  const uptime = formatUptime(snapshot.uptime_seconds);

  const cpuPercent = snapshot.cpu.percent;
  const memoryPercent = memory?.percent ?? null;
  const diskPercent = disk?.percent ?? null;
  const loadPercent = load?.percent ?? null;

  return (
    <>
      <div>
        <p className="text-sm font-semibold">{snapshot.label}</p>
        <p className="text-xs text-muted-foreground">
          Updated {secondsAgo}s ago{uptime ? ` \u00b7 up ${uptime}` : ""}
        </p>
      </div>

      <div className="grid gap-3 sm:grid-cols-2 xl:grid-cols-4">
        <MetricCard
          metric="cpu"
          title="CPU"
          percent={cpuPercent}
          level={levelFor(cpuPercent, warn, critical)}
          detail={`${snapshot.cpu.cores} cores`}
          history={history.cpu}
        />
        <MetricCard
          metric="memory"
          title="Memory"
          percent={memoryPercent}
          level={levelFor(memoryPercent, warn, critical)}
          detail={memory ? `${formatBytes(memory.used_bytes)} of ${formatBytes(memory.total_bytes)}` : ""}
          history={history.memory}
        />
        <MetricCard
          metric="disk"
          title="Disk"
          percent={diskPercent}
          level={levelFor(diskPercent, warn, critical)}
          detail={disk ? `${formatBytes(disk.used_bytes)} of ${formatBytes(disk.total_bytes)}` : ""}
          history={history.disk}
        />
        <MetricCard
          metric="load"
          title="Load"
          percent={loadPercent}
          level={levelFor(loadPercent, warn, critical)}
          detail={load ? `load ${load.one} / ${load.cores} cores` : ""}
          history={history.load}
        />
      </div>

      <div className="space-y-2">
        <p className="text-xs font-semibold uppercase tracking-wide text-muted-foreground">Apps</p>
        {!apps.available ? (
          <ConsoleEmpty>{"Per-app usage isn't set up on this server yet"}</ConsoleEmpty>
        ) : (
          <>
            {apps.stale ? (
              <p className="text-xs" style={{ color: LEVEL_COLORS.warn }}>
                Per-app numbers are {Math.max(1, Math.round((apps.age_seconds ?? 0) / 60))} minutes old
              </p>
            ) : null}
            {apps.containers.length > 0 ? (
              <div className="overflow-x-auto rounded-[var(--cx-r-md,14px)] border border-border">
                <table className="w-full min-w-[640px] border-collapse text-[13px]">
                  <thead>
                    <tr className="bg-muted text-muted-foreground">
                      <th className="px-3 py-2 text-left font-semibold">Name</th>
                      <th className="px-3 py-2 text-right font-semibold">CPU %</th>
                      <th className="px-3 py-2 text-right font-semibold">Memory used / limit</th>
                      <th className="px-3 py-2 text-right font-semibold">Mem %</th>
                      <th className="px-3 py-2 text-right font-semibold">PIDs</th>
                    </tr>
                  </thead>
                  <tbody>
                    {apps.containers.map((app: ServerApp) => (
                      <tr key={app.name} className="border-t border-border">
                        <td className="px-3 py-2 text-left font-medium">{app.name}</td>
                        <td className="px-3 py-2 text-right">{formatPercent(app.cpu_percent)}</td>
                        <td className="px-3 py-2 text-right">
                          {formatBytes(app.mem_used_bytes)} / {formatBytes(app.mem_limit_bytes)}
                        </td>
                        <td className="px-3 py-2 text-right">{formatPercent(app.mem_percent)}</td>
                        <td className="px-3 py-2 text-right">
                          {app.pids == null ? "\u2014" : app.pids}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            ) : (
              <ConsoleEmpty>No running apps reported.</ConsoleEmpty>
            )}
          </>
        )}
      </div>
    </>
  );
}

export function ServerSection(): JSX.Element {
  const { api } = useAuth();

  const snapshotQuery = useQuery({
    queryKey: serverSnapshotQueryKey,
    queryFn: () => fetchServerSnapshot(api),
    refetchInterval: 5000,
  });

  const snapshot = snapshotQuery.data ?? null;

  const [history, setHistory] = React.useState<History>(EMPTY_HISTORY);
  const lastSampledAt = React.useRef<string | null>(null);

  React.useEffect(() => {
    if (snapshot == null || snapshot.sampled_at === lastSampledAt.current) return;
    lastSampledAt.current = snapshot.sampled_at;
    setHistory((prev) => ({
      cpu: appendPoint(prev.cpu, snapshot.cpu.percent),
      memory: appendPoint(prev.memory, snapshot.memory?.percent ?? null),
      disk: appendPoint(prev.disk, snapshot.disk?.percent ?? null),
      load: appendPoint(prev.load, snapshot.load?.percent ?? null),
    }));
  }, [snapshot]);

  const warn = snapshot?.thresholds.warn ?? 80;
  const critical = snapshot?.thresholds.critical ?? 90;

  return (
    <SurfaceCard className="space-y-3">
      <SectionLabel>Server</SectionLabel>

      {snapshotQuery.isPending ? (
        <Spinner label="Loading server stats" />
      ) : snapshotQuery.isError ? (
        <p role="alert" className="text-sm text-destructive">
          {mutationErrorMessage(snapshotQuery.error)}
        </p>
      ) : snapshot == null ? (
        <ConsoleEmpty>No server stats yet.</ConsoleEmpty>
      ) : (
        <ServerBody snapshot={snapshot} history={history} warn={warn} critical={critical} />
      )}
    </SurfaceCard>
  );
}
