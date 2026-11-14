import { describe, expect, it } from "vitest";
import { describeError } from "./errors";

describe("describeError", () => {
  it("uses the message of a CommandError instead of printing [object Object]", () => {
    const text = describeError({ code: "VALIDATION_ERROR", message: "Name is required." });
    expect(text).toBe("Name is required.");
    expect(String({ code: "x", message: "y" })).toBe("[object Object]"); // the bug this guards against
  });

  it("explains an unreachable backend in plain language", () => {
    expect(describeError({ code: "SIDECAR_UNAVAILABLE", message: "request to sidecar failed: refused" })).toMatch(
      /backend is not responding/i,
    );
    expect(describeError({ code: "SIDECAR_STARTUP_FAILED", message: "x" })).toMatch(/not responding/i);
  });

  it("adds the correlation id so support can find the log line", () => {
    expect(describeError({ code: "INTERNAL_ERROR", message: "Oops.", correlation_id: "abc-123" })).toBe(
      "Oops. (reference abc-123)",
    );
  });

  it("copes with strings and unknown values", () => {
    expect(describeError("plain")).toBe("plain");
    expect(describeError(undefined)).toBe("An unexpected error occurred.");
    expect(describeError({ code: "X", message: "" })).toBe("An unexpected error occurred.");
  });
});
