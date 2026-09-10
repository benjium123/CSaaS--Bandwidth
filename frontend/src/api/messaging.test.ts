import { describe, expect, it } from "vitest";
import {
  MAX_MEDIA_BYTES,
  attachmentLimitSentence,
  mediaRejectionReason,
  trackableUrls,
  datetimeLocalMin,
  scheduleRejectionReason,
  toIsoWithOffset,
} from "./messaging";

function localDateTimeValue(date: Date): string {
  return `${date.getFullYear()}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}T${String(date.getHours()).padStart(2, "0")}:${String(date.getMinutes()).padStart(2, "0")}`;
}

describe("messaging helpers", () => {
  it("advertises an attachment ceiling the server would allow", () => {
    expect(attachmentLimitSentence()).toBe("Attachments up to 3.7 MB");

    const match = attachmentLimitSentence().match(/up to ([\d.]+) MB/);
    expect(Number(match?.[1])).toBeLessThanOrEqual(MAX_MEDIA_BYTES / 1_000_000);
  });

  it("returns the right media rejection reason", () => {
    expect(
      mediaRejectionReason({ type: "image/png", size: 10, name: "small.png" }),
    ).toBeNull();

    expect(
      mediaRejectionReason({
        type: "image/png",
        size: MAX_MEDIA_BYTES + 1,
        name: "big.png",
      }),
    ).toBe(attachmentLimitSentence());

    expect(
      mediaRejectionReason({
        type: "application/zip",
        size: 10,
        name: "archive.zip",
      }),
    ).toBe("That kind of file can't be sent in a message.");
  });

  it("finds only trackable urls and strips trailing punctuation like the server", () => {
    expect(trackableUrls("")).toEqual([]);
    expect(trackableUrls("example.com")).toEqual([]);

    expect(trackableUrls("go to https://example.com/x now")).toEqual([
      "https://example.com/x",
    ]);

    expect(trackableUrls("see https://example.com/x.")).toEqual([
      "https://example.com/x",
    ]);

    expect(
      trackableUrls("see https://example.com/a or https://example.com/b"),
    ).toEqual(["https://example.com/a", "https://example.com/b"]);

    expect(trackableUrls("https://example.com/a).")).toEqual([
      "https://example.com/a",
    ]);
  });

  it("builds datetime-local minimum from local date parts", () => {
    const date = new Date(2025, 0, 2, 3, 4, 5);
    expect(datetimeLocalMin(date)).toBe("2025-01-02T03:04");
  });

  it("accepts or rejects schedule times correctly", () => {
    const now = new Date();

    expect(scheduleRejectionReason("", now)).toBeNull();

    const past = new Date(Date.now() - 24 * 60 * 60 * 1000);
    expect(scheduleRejectionReason(localDateTimeValue(past), now)).toBe(
      "Pick a time in the future to send this later",
    );

    const future = new Date(Date.now() + 60 * 60 * 1000);
    expect(scheduleRejectionReason(localDateTimeValue(future), now)).toBeNull();

    const farFuture = new Date(Date.now() + 200 * 24 * 60 * 60 * 1000);
    expect(scheduleRejectionReason(localDateTimeValue(farFuture), now)).toBe(
      "Messages can be scheduled up to 90 days ahead",
    );
  });

  it("converts local datetime values to absolute ISO instants", () => {
    expect(toIsoWithOffset("")).toBe("");

    const value = "2025-01-02T03:04";
    expect(toIsoWithOffset(value)).toBe(new Date(value).toISOString());
  });
});
