import * as React from "react";
import { Moon, Sun } from "lucide-react";
import "@fontsource-variable/archivo";
import "@fontsource-variable/onest";
import "@fontsource-variable/martian-mono";
import { useAuth } from "@/auth/AuthContext";
import { surfaceThemeClass, useSurfaceTheme } from "@/auth/useSurfaceTheme";
import "./journey.css";

export const JOURNEY_STEPS = ["Verify", "Review", "Numbers", "Inbox"] as const;

/** The customer's signup-to-inbox path in the landing page's brand, light or dark. */
export function JourneyShell({
  step,
  wide = false,
  children,
}: {
  /** 0 Verify · 1 Review · 2 Numbers · 3 Inbox */
  step: number;
  wide?: boolean;
  children: React.ReactNode;
}) {
  const { theme, toggle } = useSurfaceTheme();
  const { logout } = useAuth();
  return (
    <div className={`rj console-surface ${surfaceThemeClass(theme)}`}>
      <header className="rj-head">
        <a className="rj-logo" href="/" aria-label="Ringlite home">
          <span className="rj-mark" aria-hidden="true"><span /><span /><span /></span>
          ringlite
        </a>
        <div className="rj-head-actions">
          <button
            type="button"
            className="rj-round"
            onClick={toggle}
            aria-label={theme === "dark" ? "Switch to the light theme" : "Switch to the dark theme"}
          >
            {theme === "dark" ? <Sun size={16} /> : <Moon size={16} />}
          </button>
          <button type="button" className="rj-ghost" onClick={() => logout()}>
            Sign out
          </button>
        </div>
      </header>
      <main className="rj-main" data-wide={wide || undefined}>
        <ol className="rj-steps" aria-label="Your progress">
          {JOURNEY_STEPS.map((label, i) => (
            <li key={label} data-state={i < step ? "done" : i === step ? "now" : "next"} aria-current={i === step ? "step" : undefined}>
              <span>
                {i + 1}. {label}
              </span>
            </li>
          ))}
        </ol>
        {children}
      </main>
    </div>
  );
}
