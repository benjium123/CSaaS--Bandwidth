/* SCRATCH - untracked design preview. Mounts the real public + journey screens against a
   stub API so the design can be looked at without a backend. Not part of the build. */
import * as React from "react";
import ReactDOM from "react-dom/client";
import { MemoryRouter } from "react-router-dom";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { AuthProvider } from "./auth/AuthContext";
import { makeStubClient } from "./test/harness";
import { OnboardingPage } from "./pages/OnboardingPage";
import { SignUpPage } from "./pages/SignUpPage";
import { LoginPage } from "./pages/LoginPage";
import type { KycProfile } from "./api/kyc";
import "./index.css";

const ME = {
  id: "u1",
  email: "ops@acme.co",
  full_name: "Ops",
  second_factor_required: false,
  memberships: [
    {
      org_id: "org-1",
      org_name: "Acme",
      org_slug: "acme",
      role_name: "admin",
      permissions: ["org:read", "org:update"],
    },
  ],
  permissions: ["org:read", "org:update"],
};

const BASE: KycProfile = {
  status: "draft",
  business: {
    country: null, legal_name: null, dba_name: null, entity_type: null,
    registration_number: null, tax_id: null, incorporation_date: null,
    registered_address: null, operating_address: null, website: null,
    business_email: null, business_phone: null,
  },
  use_case: null,
  use_case_pending: null,
  persons: [],
  documents: [],
  checks: {},
  agreement: { current_version: "1", accepted_version: null, accepted_at: null },
  missing: [],
  info_request: null,
  submitted_at: null,
  decided_at: null,
  decision_reason: null,
  limits: null,
  deposit_required_cents: null,
  next_reverification_at: null,
};

const owner = (over: Record<string, unknown> = {}) => ({
  id: "p1", role: "owner", full_name: "Jane Smith", email: null, ownership_percent: 100,
  is_user: true, is_you: true, status: "verified", verified_name: "Jane Smith",
  document_country: "US", verified_at: "2025-09-01T00:00:00+00:00", last_error: null,
  residential_address: null, ...over,
}) as KycProfile["persons"][number];

type Scene = KycProfile | "landing" | "signup" | "login";

const SCENES: Record<string, Scene> = {
  landing: "landing",
  signup: "signup",
  login: "login",
  draft: { ...BASE, status: "draft", missing: ["legal_name", "registration_number", "use_case.description", "owner", "id_verification", "documents", "agreement"] },
  "draft-nearly": { ...BASE, status: "draft", missing: ["agreement"] },
  ready: { ...BASE, status: "draft", missing: [] },
  needs_info: {
    ...BASE, status: "needs_info", missing: ["proof_of_address"],
    info_request: "The utility bill you sent is dated March and we need one from the last 90 days. A bank statement or council tax letter works too.",
  },
  in_review: { ...BASE, status: "in_review", submitted_at: "2026-09-14T00:00:00+00:00" },
  approved: {
    ...BASE, status: "approved", decided_at: "2026-09-16T00:00:00+00:00",
    limits: { daily_calls: 300, daily_texts: 800, max_numbers: 3 },
  },
  rejected: {
    ...BASE, status: "rejected", decided_at: "2026-09-16T00:00:00+00:00",
    decision_reason: "The registration number given does not match any active entity in the state register, and the address on the utility bill is a mail-forwarding service.",
  },
  suspended: { ...BASE, status: "suspended" },
  reverification_due: {
    ...BASE, status: "reverification_due",
    next_reverification_at: "2026-09-01T00:00:00+00:00",
    info_request: "Annual re-verification: each owner needs to repeat the ID and selfie check within 14 days to keep calling and texting.",
    persons: [owner(), owner({ id: "p2", full_name: "Marcus Webb", is_you: false, is_user: true })],
  },
};

function Preview() {
  const [scene, setScene] = React.useState<string>(
    () => new URLSearchParams(location.search).get("s") || "landing",
  );
  const value: Scene = SCENES[scene] ?? SCENES.landing;
  const isPublic = typeof value === "string";

  const client = React.useMemo(
    () =>
      makeStubClient({
        "/api/v1/auth/me": isPublic ? new Error("no session") : ME,
        "/api/v1/kyc/profile": isPublic ? {} : value,
        "/api/v1/auth/": {},
      }),
    [scene, value, isPublic],
  );

  let body: React.ReactNode;
  // landing preview removed: the landing page now lives outside this repo
  if (value === "signup") body = <SignUpPage />;
  else if (value === "login") body = <LoginPage />;
  else body = <OnboardingPage />;

  return (
    <div style={{ minHeight: "100vh", display: "flex", flexDirection: "column" }}>
      <div
        style={{
          position: "fixed", zIndex: 500, bottom: 0, left: 0, right: 0,
          display: "flex", flexWrap: "wrap", gap: 6, padding: "8px 10px",
          background: "#0a0f11", borderTop: "1px solid #3a2415", fontFamily: "monospace",
          fontSize: 11,
        }}
      >
        {Object.keys(SCENES).map((k) => (
          <button
            key={k}
            onClick={() => {
              setScene(k);
              history.replaceState(null, "", "?s=" + k);
            }}
            style={{
              padding: "3px 8px", borderRadius: 3, cursor: "pointer",
              border: "1px solid " + (k === scene ? "#c9793a" : "#3a2415"),
              background: k === scene ? "#c9793a" : "transparent",
              color: k === scene ? "#0a0f11" : "#9a9086",
            }}
          >
            {k}
          </button>
        ))}
      </div>
      <div style={{ flex: 1, display: "flex", paddingBottom: 40 }} key={scene}>
        <QueryClientProvider client={new QueryClient({ defaultOptions: { queries: { retry: false } } })}>
          <AuthProvider client={client}>
            <MemoryRouter>{body}</MemoryRouter>
          </AuthProvider>
        </QueryClientProvider>
      </div>
    </div>
  );
}

ReactDOM.createRoot(document.getElementById("root")!).render(<Preview />);
