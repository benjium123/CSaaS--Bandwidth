/**
 * The light/dark control for the public surface.
 *
 * One component for the landing page and the authentication screens, so the front door
 * cannot end up with two controls that look and behave slightly differently. It renders a
 * real <button>, not a checkbox dressed as a switch: the thing it does is act, immediately,
 * and a switch would imply a setting that is being staged.
 *
 * THE ACCESSIBLE NAME STATES THE ACTION, NOT THE STATE. "Switch to the light theme" on a
 * dark page, rather than "Light theme", which reads as a label and leaves someone using a
 * screen reader unsure whether pressing it changes anything or merely says where they are.
 * The icon is `aria-hidden` and is never part of the name.
 */
import { Moon, Sun } from "lucide-react";
import { cn } from "@/lib/utils";
import "./themeToggle.css";
import type { SurfaceTheme } from "./useSurfaceTheme";

export function ThemeToggle({
  theme,
  onToggle,
  className,
}: {
  theme: SurfaceTheme;
  onToggle: () => void;
  className?: string;
}) {
  const goingLight = theme !== "light";
  return (
    <button
      type="button"
      className={cn("ex-theme", className)}
      onClick={onToggle}
      aria-label={goingLight ? "Switch to the light theme" : "Switch to the dark theme"}
    >
      {goingLight ? (
        <Sun aria-hidden="true" focusable="false" className="ex-theme-icon" />
      ) : (
        <Moon aria-hidden="true" focusable="false" className="ex-theme-icon" />
      )}
    </button>
  );
}
