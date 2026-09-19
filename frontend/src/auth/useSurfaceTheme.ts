/**
 * Light or dark, for the PUBLIC surface only.
 *
 * This is deliberately not the console's theme. The signed-in console has its own light
 * and dark in index.css, switched by a `.dark` class on individual panes; this hook governs
 * the front door - the landing page and the authentication screens - which is one continuous
 * designed surface and switches as a whole.
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

export function useSurfaceTheme(): {
  theme: SurfaceTheme;
  setTheme: (next: SurfaceTheme) => void;
  toggle: () => void;
} {
  const [theme, setThemeState] = React.useState<SurfaceTheme>(initial);

  const setTheme = React.useCallback((next: SurfaceTheme) => {
    setThemeState(next);
    try {
      window.localStorage.setItem(STORAGE_KEY, next);
    } catch {
      // Storage is unavailable or full. The choice still applies for this visit.
    }
  }, []);

  const toggle = React.useCallback(() => {
    // The write happens inside the updater so that the value stored is the one actually
    // committed. Computing `next` outside from a captured `theme` would persist a stale
    // value if two presses landed in the same batch.
    setThemeState((current) => {
      const next: SurfaceTheme = current === "light" ? "dark" : "light";
      try {
        window.localStorage.setItem(STORAGE_KEY, next);
      } catch {
        // See above.
      }
      return next;
    });
  }, []);

  return { theme, setTheme, toggle };
}
