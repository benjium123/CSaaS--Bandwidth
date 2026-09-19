import { Navigate } from "react-router-dom";
import { useGate } from "@/api/capabilities";
import { isOwner, useAuth } from "@/auth/AuthContext";
import { canViewSettingsSection, SETTINGS_SECTIONS } from "./settingsSections";

/** /settings → the first section this caller may open (B1, Opus P20a verify). A member
 * holding none of the section permissions goes back to the inbox instead of an
 * access-denied card. Lives in its own module so page mocks never hide it. */
export function SettingsIndexRedirect() {
  const gate = useGate();
  const { me, orgId } = useAuth();
  if (gate.isLoading) return null;
  // Same ownership read as SettingsPage: the permission string alone cannot tell an owner
  // from a non-owner admin. With the current ordering `workspace` still wins for anyone who
  // can read the org, so this changes no existing destination - it just keeps the one rule
  // in one place.
  const owner = isOwner(me, orgId);
  const first = SETTINGS_SECTIONS.find((s) => canViewSettingsSection(s, gate.can, owner));
  return <Navigate to={first ? `/settings/${first.id}` : "/inbox"} replace />;
}
