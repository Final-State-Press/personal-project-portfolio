// CommandError — the single error shape surfaced to the renderer.
// Mirrors the Rust `CommandError` and the Python `ErrorResponse`.

export type ErrorCode =
  | "VALIDATION_ERROR"
  | "NOT_FOUND"
  | "CONFLICT"
  | "ARCHIVED_READ_ONLY"
  | "AUTHENTICATION_FAILED"
  | "DATABASE_ERROR"
  | "SIDECAR_UNAVAILABLE"
  | "SIDECAR_STARTUP_FAILED"
  | "INTERNAL_ERROR";

export interface CommandError {
  code: ErrorCode | string;
  message: string;
  field_errors?: Record<string, string[]>;
  correlation_id?: string | null;
  retryable?: boolean;
}

/** Narrow an unknown thrown value to a {@link CommandError}. */
export function isCommandError(value: unknown): value is CommandError {
  return (
    typeof value === "object" &&
    value !== null &&
    "code" in value &&
    "message" in value
  );
}

/** Normalize any thrown value into a CommandError. */
export function toCommandError(value: unknown): CommandError {
  if (isCommandError(value)) return value;
  return {
    code: "INTERNAL_ERROR",
    message: typeof value === "string" ? value : "An unexpected error occurred.",
    retryable: false,
  };
}

/** Whether an error indicates the sidecar is unreachable (degraded state). */
export function isSidecarDown(err: CommandError): boolean {
  return (
    err.code === "SIDECAR_UNAVAILABLE" || err.code === "SIDECAR_STARTUP_FAILED"
  );
}

/**
 * A message fit to show a person, from anything a command can throw.
 *
 * `String(error)` on a {@link CommandError} object prints "[object Object]", so
 * views must use this instead. Adds the correlation id, when there is one, so a
 * support request can be matched to the log line.
 */
export function describeError(value: unknown): string {
  const err = toCommandError(value);
  const base = isSidecarDown(err)
    ? "The backend is not responding. Wait a moment, or restart the app."
    : err.message || "An unexpected error occurred.";
  return err.correlation_id ? `${base} (reference ${err.correlation_id})` : base;
}
