import { describe, expect, it } from "vitest";
import { curveHoverIndex } from "./curveUtils";

describe("curveHoverIndex", () => {
  it("maps the visible 5%-95% plot range to the first and last rows", () => {
    expect(curveHoverIndex(50, 1000, 30)).toBe(0);
    expect(curveHoverIndex(950, 1000, 30)).toBe(29);
  });

  it("keeps evenly spaced points aligned with the pointer", () => {
    expect(curveHoverIndex(500, 1000, 31)).toBe(15);
    expect(curveHoverIndex(275, 1000, 31)).toBe(8);
  });

  it("clamps pointer positions outside the plotted data range", () => {
    expect(curveHoverIndex(0, 1000, 30)).toBe(0);
    expect(curveHoverIndex(1000, 1000, 30)).toBe(29);
  });
});
