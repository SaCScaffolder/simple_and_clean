import { describe, expect, it } from "vitest";
import { cn } from "@/lib/utils";

describe("cn (class name helper)", () => {
  it("merges simple class names", () => {
    expect(cn("foo", "bar")).toBe("foo bar");
  });

  it("drops falsy values", () => {
    expect(cn("foo", false, null, undefined, "bar")).toBe("foo bar");
  });

  it("resolves Tailwind conflicts (later wins)", () => {
    // twMerge should resolve "p-2" vs "p-4" by keeping only the last one.
    expect(cn("p-2", "p-4")).toBe("p-4");
  });

  it("handles conditional class objects", () => {
    const isActive = true;
    expect(cn("base", { active: isActive, disabled: !isActive })).toBe(
      "base active",
    );
  });
});