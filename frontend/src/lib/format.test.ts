import { describe, expect, it } from "vitest";
import {
  AVATAR_HUE_COUNT,
  avatarHueIndex,
  initialsOf,
  normalizePhoneToE164,
  shortRelativeTime,
} from "./format";

// The conversation list's furniture, from docs/design/console-reference.html.
describe("shortRelativeTime", () => {
  // Fixed so the month/day boundary cases cannot drift with the wall clock.
  const now = new Date("2026-09-19T12:00:00Z");
  const ago = (ms: number) => new Date(now.getTime() - ms).toISOString();

  it("floors at 1m rather than saying 'now'", () => {
    expect(shortRelativeTime(ago(0), now)).toBe("1m");
    expect(shortRelativeTime(ago(30_000), now)).toBe("1m");
    expect(shortRelativeTime(ago(59_000), now)).toBe("1m");
  });

  it("counts whole minutes up to the hour", () => {
    expect(shortRelativeTime(ago(3 * 60_000), now)).toBe("3m");
    expect(shortRelativeTime(ago(12 * 60_000), now)).toBe("12m");
    expect(shortRelativeTime(ago(49 * 60_000), now)).toBe("49m");
    expect(shortRelativeTime(ago(59 * 60_000 + 59_000), now)).toBe("59m");
  });

  it("counts whole hours up to the day", () => {
    expect(shortRelativeTime(ago(3_600_000), now)).toBe("1h");
    expect(shortRelativeTime(ago(4 * 3_600_000), now)).toBe("4h");
    expect(shortRelativeTime(ago(23 * 3_600_000), now)).toBe("23h");
  });

  // The gap the operator called out: an old conversation reads "Sep 17", not a numeric
  // locale date and not "2d".
  it("switches to a short month and day once a day old", () => {
    expect(shortRelativeTime("2026-09-17T09:00:00Z", now)).toBe("Sep 17");
    expect(shortRelativeTime(ago(24 * 3_600_000), now)).toBe("Sep 18");
  });

  it("adds the year only when it is not the current one", () => {
    expect(shortRelativeTime("2025-09-17T09:00:00Z", now)).toBe("Sep 17, 2025");
  });

  it("is empty for null and for an unparseable timestamp, and never negative", () => {
    expect(shortRelativeTime(null, now)).toBe("");
    expect(shortRelativeTime("not a date", now)).toBe("");
    // Clock skew: a timestamp from the future reads as the newest thing possible.
    expect(shortRelativeTime(new Date(now.getTime() + 60_000).toISOString(), now)).toBe("1m");
  });
});

describe("initialsOf", () => {
  it("takes one letter per word from a name", () => {
    expect(initialsOf("Ada Whitlock")).toBe("AW");
    expect(initialsOf("priya raman")).toBe("PR");
    expect(initialsOf("Jean-Luc  Picard")).toBe("JP");
  });

  it("takes the first two characters of anything that is not two words", () => {
    expect(initialsOf("(512) 555-0177")).toBe("51");
    expect(initialsOf("Marcus")).toBe("MA");
  });

  it("never renders empty", () => {
    expect(initialsOf("")).toBe("?");
    expect(initialsOf("!!!")).toBe("?");
  });
});

describe("avatarHueIndex", () => {
  it("is stable for the same seed - the colour must survive a reload", () => {
    expect(avatarHueIndex("c-ada")).toBe(avatarHueIndex("c-ada"));
    expect(avatarHueIndex("+19725550199")).toBe(avatarHueIndex("+19725550199"));
  });

  it("stays inside the palette consoleTheme.css actually defines", () => {
    for (const seed of ["", "a", "+19725550199", "c-1", "c-2", "🙂", "x".repeat(400)]) {
      const hue = avatarHueIndex(seed);
      expect(Number.isInteger(hue)).toBe(true);
      expect(hue).toBeGreaterThanOrEqual(0);
      expect(hue).toBeLessThan(AVATAR_HUE_COUNT);
    }
  });

  it("spreads a realistic set of contacts over more than one hue", () => {
    const hues = new Set(
      ["c-ada", "c-marcus", "c-priya", "c-tom", "c-dana", "c-helen", "+15125550177"].map(
        avatarHueIndex,
      ),
    );
    // Not "all seven" - a hash may collide, and pinning the exact spread would pin the
    // hash. More than one is the property that matters: uniform avatars are the bug.
    expect(hues.size).toBeGreaterThan(1);
  });
});

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
