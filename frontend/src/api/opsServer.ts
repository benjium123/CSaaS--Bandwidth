/**
 * The ops console "Server" tab: whole-host resources (CPU, memory, disk, load, uptime) plus
 * per-app (per-container) usage. Backend: app/api/routes/ops_server.py +
 * app/services/server_stats.py.
 *
 * The host numbers come from /proc and the disk of "/" inside the api container, which (with no
 * lxcfs) describe the WHOLE physical host. Per-app numbers come from a host job that writes
 * `docker stats --no-stream` output to a file mounted read-only into the container.
 */
import type { ApiClient } from "./client";

export interface ServerMemory {
  total_bytes: number;
  available_bytes: number;
  used_bytes: number;
  percent: number;
}

export interface ServerDisk {
  total_bytes: number;
  used_bytes: number;
  free_bytes: number;
  percent: number;
}

export interface ServerLoad {
  one: number;
  five: number;
  fifteen: number;
  cores: number;
  percent: number;
}

export interface ServerCpu {
  percent: number | null;
  cores: number;
}

export interface ServerApp {
  name: string;
  cpu_percent: number | null;
  mem_used_bytes: number | null;
  mem_limit_bytes: number | null;
  mem_percent: number | null;
  pids: number | null;
}

export interface ServerApps {
  available: boolean;
  stale: boolean;
  updated_at: string | null;
  age_seconds: number | null;
  containers: ServerApp[];
}

export interface ServerThresholds {
  warn: number;
  critical: number;
}

export interface ServerSnapshot {
  scope: string;
  label: string;
  sampled_at: string;
  cpu: ServerCpu;
  memory: ServerMemory | null;
  disk: ServerDisk | null;
  load: ServerLoad | null;
  uptime_seconds: number | null;
  apps: ServerApps;
  thresholds: ServerThresholds;
}

export const serverSnapshotQueryKey = ["ops", "server"] as const;

export async function fetchServerSnapshot(api: ApiClient): Promise<ServerSnapshot> {
  return api.request<ServerSnapshot>("/api/v1/ops/console/server");
}
