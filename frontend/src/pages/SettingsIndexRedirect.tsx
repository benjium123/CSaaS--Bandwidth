import { Navigate } from "react-router-dom";
import { useGate } from "@/api/capabilities";
import { SETTINGS_SECTIONS } from "./settingsSections";

/** /settings → the first section this caller may open (B1, Opus P20a verify). A member
 * holding none of the section permissions goes back to the inbox instead of an
 * access-denied card. Lives in its own module so page mocks never hide it. */
export function SettingsIndexRedirect() {
  const gate = useGate();
  if (gate.isLoading) return null;
  const first = SETTINGS_SECTIONS.find((s) => gate.can(s.permission));
  return <Navigate to={first ? `/settings/${first.id}` : "/inbox"} replace />;
}
