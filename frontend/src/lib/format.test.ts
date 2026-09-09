import { describe, expect, it } from "vitest";
import { normalizePhoneToE164 } from "./format";

describe("normalizePhoneToE164", () => {
  it("passes through an already-valid E.164 number", () => {
    expect(normalizePhoneToE164("+19725550199")).toBe("+19725550199");
  });

  // Item 1: a "+"-prefixed number must normalize by stripping punctuation only - never
  // fall through to the NANP-assuming branches on a bad/foreign number.
  it("strips spaces from a '+'-prefixed international number instead of assuming NANP", () => {
    expect(normalizePhoneToE164("+49 30 123456")).toBe("+4930123456");
    expect(normalizePhoneToE164("+44 7911 123456")).toBe("+447911123456");
  });

  it("strips parens/dashes from a '+'-prefixed NANP number", () => {
    expect(normalizePhoneToE164("+1 (972) 555-0199")).toBe("+19725550199");
  });

  it("rejects a '+'-prefixed number that fails the E.164 shape, without falling back to +1", () => {
    // Too short to be a real international number - must not become "+1...".
    expect(normalizePhoneToE164("+1234")).toBeNull();
  });

  it("assumes NANP for a bare 10-digit number", () => {
    expect(normalizePhoneToE164("9725550199")).toBe("+19725550199");
  });

  it("assumes NANP for an 11-digit number starting with 1", () => {
    expect(normalizePhoneToE164("19725550199")).toBe("+19725550199");
  });

  // Item 2: reject free text with embedded digits.
  it("rejects free text that merely contains digits", () => {
    expect(normalizePhoneToE164("call 9725550199 now")).toBeNull();
    expect(normalizePhoneToE164("ext 199")).toBeNull();
  });

  it("accepts common punctuation (parens, dashes, dots, spaces) around plain digits", () => {
    expect(normalizePhoneToE164("(972) 555-0199")).toBe("+19725550199");
    expect(normalizePhoneToE164("972.555.0199")).toBe("+19725550199");
  });

  // Item 2: NANP area code / exchange can't start with 0 or 1.
  it("rejects a 10-digit number whose area code starts with 0 or 1", () => {
    expect(normalizePhoneToE164("0725550199")).toBeNull();
    expect(normalizePhoneToE164("1725550199")).toBeNull();
  });

  it("rejects a 10-digit number whose exchange starts with 0 or 1", () => {
    expect(normalizePhoneToE164("9720550199")).toBeNull();
    expect(normalizePhoneToE164("9721550199")).toBeNull();
  });

  it("rejects an 11-digit (leading 1) number whose national part fails the NANP prefix check", () => {
    expect(normalizePhoneToE164("10725550199")).toBeNull();
    expect(normalizePhoneToE164("19721550199")).toBeNull();
  });

  it("rejects unparseable input", () => {
    expect(normalizePhoneToE164("")).toBeNull();
    expect(normalizePhoneToE164("12345")).toBeNull();
    expect(normalizePhoneToE164("+")).toBeNull();
  });
});
