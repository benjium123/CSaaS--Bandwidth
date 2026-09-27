import { describe, expect, it } from "vitest";
import { textingLocked, type OrgCapabilities } from "./capabilities";

const org = (over: Partial<OrgCapabilities>): OrgCapabilities => ({
  has_provider: true,
  has_number: true,
  member_count: 1,
  registration_state: "approved",
  account_type: "individual",
  kyc_status: "approved",
  onboarding_step: "ready",
  calling_ready: true,
  messaging_ready: true,
  ...over,
});

describe("textingLocked (individual 10DLC hard gate)", () => {
  it("locks an individual account until its 10DLC is approved", () => {
    expect(textingLocked(org({ messaging_ready: false, registration_state: "pending" }))).toBe(true);
    expect(textingLocked(org({ messaging_ready: false, registration_state: "unknown" }))).toBe(true);
  });

  it("unlocks an individual account once its 10DLC is approved", () => {
    expect(textingLocked(org({}))).toBe(false);
  });

  it("never locks a business account (the carrier is its gate)", () => {
    expect(textingLocked(org({ account_type: "business", messaging_ready: false }))).toBe(false);
  });

  it("leaves texting on while capabilities are unknown (the server still refuses)", () => {
    expect(textingLocked(undefined)).toBe(false);
    expect(textingLocked(null)).toBe(false);
  });
});
