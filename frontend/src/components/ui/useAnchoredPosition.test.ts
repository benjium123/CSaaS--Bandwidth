import { describe, expect, it } from "vitest";

import { anchoredBox } from "./useAnchoredPosition";

describe("anchoredBox", () => {
  it("drops the panel 8px under the anchor and right-aligns it", () => {
    expect(anchoredBox({ bottom: 50, right: 1000 }, 320, 1024)).toEqual({ top: 58, left: 680 });
  });

  it("pulls the panel back inside the viewport when the anchor is near the right edge", () => {
    expect(anchoredBox({ bottom: 50, right: 1020 }, 320, 1024)).toEqual({ top: 58, left: 696 });
  });

  it("never lets the panel start left of the 8px gutter", () => {
    expect(anchoredBox({ bottom: 50, right: 100 }, 320, 1024)).toEqual({ top: 58, left: 8 });
  });
});
