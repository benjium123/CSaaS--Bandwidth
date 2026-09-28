import { beforeEach, describe, expect, it } from "vitest";
import { act, renderHook } from "@testing-library/react";
import {
  COLUMN_LIMITS,
  WIDTHS_STORAGE_KEY,
  clampWidth,
  useColumnWidths,
} from "./useColumnWidths";

const DEFAULTS = { inbox: 256, list: 320 };

describe("clampWidth", () => {
  it("clamps to the column limits", () => {
    expect(clampWidth("inbox", 50)).toBe(COLUMN_LIMITS.inbox.min);
    expect(clampWidth("inbox", 10_000)).toBe(COLUMN_LIMITS.inbox.max);
    expect(clampWidth("list", 5)).toBe(COLUMN_LIMITS.list.min);
  });

  it("falls back to the default for a non-numeric width", () => {
    expect(clampWidth("inbox", Number.NaN)).toBe(COLUMN_LIMITS.inbox.default);
  });
});

describe("useColumnWidths", () => {
  beforeEach(() => {
    window.localStorage.clear();
  });

  it("starts at the defaults", () => {
    const { result } = renderHook(() => useColumnWidths());
    expect(result.current.widths).toEqual(DEFAULTS);
    expect(result.current.closed).toEqual({ inbox: false, list: false });
  });

  it("clamps a width on set", () => {
    const { result } = renderHook(() => useColumnWidths());
    act(() => result.current.setWidth("inbox", 10));
    expect(result.current.widths.inbox).toBe(COLUMN_LIMITS.inbox.min);
    act(() => result.current.setWidth("inbox", 10_000));
    expect(result.current.widths.inbox).toBe(COLUMN_LIMITS.inbox.max);
  });

  it("persists and restores widths across mounts", () => {
    const first = renderHook(() => useColumnWidths());
    act(() => first.result.current.setWidth("list", 400));
    first.unmount();

    const stored = JSON.parse(window.localStorage.getItem(WIDTHS_STORAGE_KEY) ?? "null");
    expect(stored.widths.list).toBe(400);

    const second = renderHook(() => useColumnWidths());
    expect(second.result.current.widths.list).toBe(400);
  });

  it("toggles a column closed and open again", () => {
    const { result } = renderHook(() => useColumnWidths());
    act(() => result.current.toggle("inbox"));
    expect(result.current.closed.inbox).toBe(true);
    act(() => result.current.setClosed("inbox", false));
    expect(result.current.closed.inbox).toBe(false);
  });

  it("ignores unparseable JSON and falls back to defaults", () => {
    window.localStorage.setItem(WIDTHS_STORAGE_KEY, "{ this is not json");
    const { result } = renderHook(() => useColumnWidths());
    expect(result.current.widths).toEqual(DEFAULTS);
    expect(result.current.closed).toEqual({ inbox: false, list: false });
  });

  it("clamps stored widths on load", () => {
    window.localStorage.setItem(
      WIDTHS_STORAGE_KEY,
      JSON.stringify({ widths: { inbox: 5_000, list: 1 }, closed: { inbox: true, list: false } }),
    );
    const { result } = renderHook(() => useColumnWidths());
    expect(result.current.widths).toEqual({
      inbox: COLUMN_LIMITS.inbox.max,
      list: COLUMN_LIMITS.list.min,
    });
    expect(result.current.closed).toEqual({ inbox: true, list: false });
  });
});
