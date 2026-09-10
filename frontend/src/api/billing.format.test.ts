import { describe, expect, it } from "vitest";
import {
  formatCredits,
  formatQuantity,
  formatRateUnit,
  formatUnitPrice,
  lastTopupMicros,
  metricLabel,
  parseDollarsToMicros,
  rateDisplayMicros,
  usageLines,
  usageTotalMicros,
  WARNING_COPY,
  type BillingSummary,
  type UsageSummary,
} from "./billing";

describe("billing formatters", () => {
  it("formats credits with cent rounding", () => {
    expect(formatCredits(0)).toBe("$0.00");
    expect(formatCredits(1_000_000)).toBe("$1.00");
    expect(formatCredits(1_005_000)).toBe("$1.01");
    expect(formatCredits(-1_230_000)).toBe("-$1.23");
    expect(formatCredits(12_345_678_000)).toBe("$12,345.68");
    expect(formatCredits(4_999)).toBe("$0.00");
    expect(formatCredits(5_000)).toBe("$0.01");
  });

  it("formats unit prices without throwing on sub-cent values", () => {
    expect(formatUnitPrice(0)).toBe("$0.00");
    expect(formatUnitPrice(18)).toBe("$0.000018");
    expect(formatUnitPrice(1_000_000)).toBe("$1.00");
    expect(formatUnitPrice(1.9999999)).toBe("$0.000002");
  });

  it("parses dollars to micros and rejects negative, zero, and over-precise amounts", () => {
    expect(parseDollarsToMicros("25")).toBe(25_000_000);
    expect(parseDollarsToMicros("37.50")).toBe(37_500_000);
    expect(parseDollarsToMicros(" 10 ")).toBe(10_000_000);
    expect(parseDollarsToMicros("")).toBeNull();
    expect(parseDollarsToMicros("abc")).toBeNull();
    expect(parseDollarsToMicros("-5")).toBeNull();
    expect(parseDollarsToMicros("0")).toBeNull();
    expect(parseDollarsToMicros("1.234")).toBeNull();
  });

  it("recognizes both last-topup shapes", () => {
    expect(lastTopupMicros(undefined)).toBe(0);

    const numberLastTopup: BillingSummary = {
      balance_micros: 0,
      reserved_micros: 0,
      warning: null,
      auto_recharge: null,
      last_topup: 42_000_000,
    };
    expect(lastTopupMicros(numberLastTopup)).toBe(42_000_000);

    const objectLastTopup: BillingSummary = {
      ...numberLastTopup,
      last_topup: { amount_micros: 7_000_000, created_at: null },
    };
    expect(lastTopupMicros(objectLastTopup)).toBe(7_000_000);

    const nullLastTopup: BillingSummary = {
      ...numberLastTopup,
      last_topup: null,
    };
    expect(lastTopupMicros(nullLastTopup)).toBe(0);
  });

  it("formats quantities as minutes, characters, tokens, or a plain number", () => {
    expect(formatQuantity("ai_voice_seconds", 750)).toBe("12.5 minutes");
    expect(formatQuantity("stt_seconds", 60)).toBe("1.0 minutes");
    expect(formatQuantity("tts_characters", 1234)).toBe("1,234 characters");
    expect(formatQuantity("llm_tokens_in", 2000)).toBe("2,000 tokens");
    expect(formatQuantity("unknown_metric", 42)).toBe("42");
  });

  it("labels known metrics and replaces underscores for unknown ones", () => {
    expect(metricLabel("ai_voice_seconds")).toBe("Assistant minutes");
    expect(metricLabel("stt_seconds")).toBe("Speech recognition");
    expect(metricLabel("tts_characters")).toBe("Voice generation");
    expect(metricLabel("llm_tokens_in")).toBe("Language model input");
    expect(metricLabel("llm_tokens_out")).toBe("Language model output");
    expect(metricLabel("my_metric")).toBe("my metric");
  });

  it("converts displayed unit rates and labels per metric", () => {
    expect(rateDisplayMicros("ai_voice_seconds", 300)).toBe(18_000);
    expect(formatRateUnit("ai_voice_seconds")).toBe("per minute");

    expect(rateDisplayMicros("tts_characters", 2)).toBe(2_000);
    expect(formatRateUnit("tts_characters")).toBe("per 1,000 characters");

    expect(rateDisplayMicros("llm_tokens_in", 3)).toBe(3_000);
    expect(formatRateUnit("llm_tokens_in")).toBe("per 1,000 tokens");

    expect(rateDisplayMicros("unknown_metric", 123.4)).toBe(123);
    expect(formatRateUnit("unknown_metric")).toBe("per unit");
  });

  it("usageLines matches array and map shapes, drops zero lines, and sorts by price", () => {
    const arraySummary: UsageSummary = {
      by_metric: [
        { metric: "a", quantity: 10, price_micros: 1000 },
        { metric: "b", quantity: 5, price_micros: 2000 },
        { metric: "drop", quantity: 0, price_micros: 0 },
      ],
    };

    const mapSummary: UsageSummary = {
      by_metric: {
        a: { quantity: 10, price_micros: 1000 },
        b: { quantity: 5, price_micros: 2000 },
        drop: { quantity: 0, price_micros: 0 },
      },
    };

    expect(usageLines(arraySummary)).toEqual(usageLines(mapSummary));
    expect(usageLines(arraySummary).map((line) => line.metric)).toEqual(["b", "a"]);
  });

  it("usageTotalMicros prefers total_price_micros and falls back to line totals", () => {
    const withTotal: UsageSummary = {
      total_price_micros: 999,
      by_metric: { a: { quantity: 1, price_micros: 5 } },
    };
    expect(usageTotalMicros(withTotal)).toBe(999);

    const fallback: UsageSummary = {
      by_metric: [
        { metric: "a", quantity: 1, price_micros: 1000 },
        { metric: "b", quantity: 1, price_micros: 2000 },
      ],
    };
    expect(usageTotalMicros(fallback)).toBe(3000);
    expect(usageTotalMicros(undefined)).toBe(0);
  });

  it("warning copy has all levels and avoids billing jargon", () => {
    const texts = Object.values(WARNING_COPY).flatMap((copy) => [copy.title, copy.body]);
    expect(texts).toHaveLength(6);

    for (const text of texts) {
      expect(text.trim()).not.toBe("");
      expect(text.toLowerCase()).not.toContain("micros");
      expect(text.toLowerCase()).not.toContain("cost");
      expect(text.toLowerCase()).not.toContain("balance");
    }
  });
});
