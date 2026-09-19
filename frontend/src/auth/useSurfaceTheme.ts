/**
 * Light or dark, for the WHOLE product.
 *
 * It used to say "for the public surface only", and that sentence was the bug. The front
 * door defaulted to light and the console was hardcoded dark, so signing in flipped the
 * product to dark underneath the person who had just chosen light. There is now ONE stored
 * preference, under ONE key, and both surfaces read it through this hook. The console's
 * paper values are lifted from authTheme.light.css rather than re-derived, for the same
 * reason: one product, one paper.
 *
 * WHY THIS IS A MODULE-LEVEL STORE AND NOT PLAIN useState
 * The public surface had exactly one consumer per screen, so per-component state was fine.
 * The console has eleven - the Shell, both halves of the Sidebar, four pages, the command
 * palette, both softphone wrappers and the step-up dialog - and the toggle lives inside one
 * of them. With useState, pressing it would have re-rendered the Sidebar and left the other
 * ten on their old value: a console that is half light and half dark, which is worse than
 * either. `useSyncExternalStore` over a single module-level value means every consumer is
 * reading the same variable and all of them re-render on the same commit. The storage event
 * is wired in too, so a second tab follows rather than drifting.
 *
 * THE ORDER OF PREFERENCE is: what this person last chose, then light.
 *
 * The operating system is deliberately NOT consulted, and that is a decision rather than an
 * omission - it was considered and rejected by the operator. The front door is a designed
 * first impression and it is light, the way every comparable product's is; a visitor whose
 * laptop happens to be in dark mode should still meet the page as it was composed, and the
 * control in the corner is one click away. Once they use that control the choice is theirs
 * and is remembered. If this is ever reversed, `prefers-color-scheme: light` is the query
 * to read and the rule to restore is "stored, then system, then a default".
 *
 * `localStorage` throws outright on access in some privacy modes, so both the read and the
 * write are wrapped. A failed read means "no stored choice", and a failed write means the
 * choice lasts for this visit only - neither is worth an error.
 *
 * Nothing here touches <html> or <body>. The class goes on the surface wrapper, so a light
 * landing page cannot reach into the console's own styling, and an unmounted public screen
 * leaves no trace behind it.
 */
import * as React from "react";

export type SurfaceTheme = "dark" | "light";

const STORAGE_KEY = "csaas.surface-theme";

const DEFAULT_THEME: SurfaceTheme = "light";

function readStored(): SurfaceTheme | null {
  try {
    const v = window.localStorage.getItem(STORAGE_KEY);
    return v === "light" || v === "dark" ? v : null;
  } catch {
    return null;
  }
}

/** The theme to paint on the very first render, with no flash and no effect pass. */
function initial(): SurfaceTheme {
  if (typeof window === "undefined") return DEFAULT_THEME;
  return readStored() ?? DEFAULT_THEME;
}

/**
 * The class to put on the surface wrapper, alongside `auth-surface`.
 *
 * `dark` is NOT decorative and must keep being emitted in the dark case: authTheme.css
 * explains that the wrapper carries it so that any shadcn primitive arriving with a
 * `dark:` variant behaves correctly on these screens. `is-light` is what authTheme.light.css
 * hangs every one of its overrides off, and the two are mutually exclusive by construction
 * here rather than by discipline at each call site.
 */
export function surfaceThemeClass(theme: SurfaceTheme): string {
  return theme === "light" ? "is-light" : "dark";
}

/* The store ------------------------------------------------------------------
 * One variable, one set of listeners. `current` is resolved lazily rather than at module
 * load so that importing this file has no side effect in an environment with no `window`,
 * and so that a test which seeds localStorage before rendering is still read correctly. */

let current: SurfaceTheme | null = null;
const listeners = new Set<() => void>();
let storageBound = false;

function snapshot(): SurfaceTheme {
  if (current === null) current = initial();
  return current;
}

/** The snapshot on the server, where there is no storage and nothing to be consistent with. */
function serverSnapshot(): SurfaceTheme {
  return DEFAULT_THEME;
}

function emit(next: SurfaceTheme) {
  if (current === next) return;
  current = next;
  for (const listener of listeners) listener();
}

function persist(next: SurfaceTheme) {
  try {
    window.localStorage.setItem(STORAGE_KEY, next);
  } catch {
    // Storage is unavailable or full. The choice still applies for this visit.
  }
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  // Bound once, on the first subscriber, and never removed: the handler is cheap, it is the
  // same function for the life of the page, and unbinding it when the last consumer
  // unmounts would mean a second tab's change silently not applying to a page that is about
  // to mount a consumer again.
  if (!storageBound && typeof window !== "undefined") {
    storageBound = true;
    window.addEventListener("storage", (event) => {
      if (event.key !== STORAGE_KEY) return;
      // A `null` newValue means the key was cleared, which is "no stored choice".
      const next = event.newValue;
      emit(next === "light" || next === "dark" ? next : DEFAULT_THEME);
    });
  }
  return () => {
    listeners.delete(listener);
  };
}

/** Set the preference from anywhere, including outside React. */
export function setSurfaceTheme(next: SurfaceTheme) {
  persist(next);
  emit(next);
}

export function useSurfaceTheme(): {
  theme: SurfaceTheme;
  setTheme: (next: SurfaceTheme) => void;
  toggle: () => void;
} {
  const theme = React.useSyncExternalStore(subscribe, snapshot, serverSnapshot);

  const setTheme = React.useCallback((next: SurfaceTheme) => {
    setSurfaceTheme(next);
  }, []);

  const toggle = React.useCallback(() => {
    // Read through `snapshot()` rather than from the `theme` this render closed over, so
    // that two presses landing in the same batch cannot persist a stale value.
    setSurfaceTheme(snapshot() === "light" ? "dark" : "light");
  }, []);

  return { theme, setTheme, toggle };
}

/**
 * Drop the resolved value so the next read goes back to storage. For tests only: the store
 * outlives a single `render()`, and without this a test that seeds a different preference
 * would read the value left behind by the test before it.
 */
export function __resetSurfaceThemeForTests() {
  current = null;
  for (const listener of listeners) listener();
}
