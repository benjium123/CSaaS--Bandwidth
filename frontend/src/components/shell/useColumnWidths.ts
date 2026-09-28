/**
 * The persisted widths (and collapsed flags) of the two resizable inbox columns.
 *
 * Storage is a cache, never a requirement: every read and write is guarded, and a value
 * that is missing, unparseable or out of range resolves to a sane one rather than throwing
 * on a page that has nothing to do with columns. Widths are clamped on the way IN as well
 * as on the way out, so a hand-edited storage entry cannot produce a 5000px column.
 */
import * as React from "react";

export type ColumnKey = "inbox" | "list";

export const COLUMN_LIMITS: Record<ColumnKey, { min: number; max: number; default: number }> = {
  inbox: { min: 200, max: 380, default: 256 },
  list: { min: 260, max: 520, default: 320 },
};

export const WIDTHS_STORAGE_KEY = "ringlite.inbox.widths";

type ColumnState = {
  widths: Record<ColumnKey, number>;
  closed: Record<ColumnKey, boolean>;
};

export function clampWidth(key: ColumnKey, px: number): number {
  const { min, max, default: fallback } = COLUMN_LIMITS[key];
  if (!Number.isFinite(px)) return fallback;
  return Math.min(max, Math.max(min, px));
}

function defaultState(): ColumnState {
  return {
    widths: { inbox: COLUMN_LIMITS.inbox.default, list: COLUMN_LIMITS.list.default },
    closed: { inbox: false, list: false },
  };
}

function readStored(): ColumnState {
  const fallback = defaultState();
  try {
    const raw = window.localStorage.getItem(WIDTHS_STORAGE_KEY);
    if (!raw) return fallback;

    const parsed = JSON.parse(raw) as Partial<ColumnState> | null;
    if (!parsed || typeof parsed !== "object") return fallback;

    return {
      widths: {
        inbox: clampWidth("inbox", Number(parsed.widths?.inbox ?? fallback.widths.inbox)),
        list: clampWidth("list", Number(parsed.widths?.list ?? fallback.widths.list)),
      },
      closed: {
        inbox: parsed.closed?.inbox === true,
        list: parsed.closed?.list === true,
      },
    };
  } catch {
    return fallback;
  }
}

function writeStored(state: ColumnState): void {
  try {
    window.localStorage.setItem(WIDTHS_STORAGE_KEY, JSON.stringify(state));
  } catch {
    // Storage can be unavailable (private mode, quota). The widths then live for the session.
  }
}

export function useColumnWidths(): {
  widths: Record<ColumnKey, number>;
  closed: Record<ColumnKey, boolean>;
  setWidth: (key: ColumnKey, px: number) => void;
  toggle: (key: ColumnKey) => void;
  setClosed: (key: ColumnKey, closed: boolean) => void;
} {
  const [state, setState] = React.useState<ColumnState>(() => readStored());

  React.useEffect(() => {
    writeStored(state);
  }, [state]);

  const setWidth = React.useCallback((key: ColumnKey, px: number) => {
    setState((prev) => ({
      ...prev,
      widths: { ...prev.widths, [key]: clampWidth(key, px) },
    }));
  }, []);

  const toggle = React.useCallback((key: ColumnKey) => {
    setState((prev) => ({ ...prev, closed: { ...prev.closed, [key]: !prev.closed[key] } }));
  }, []);

  const setClosed = React.useCallback((key: ColumnKey, closed: boolean) => {
    setState((prev) => ({ ...prev, closed: { ...prev.closed, [key]: closed } }));
  }, []);

  return { widths: state.widths, closed: state.closed, setWidth, toggle, setClosed };
}
