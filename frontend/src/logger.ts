/** Structured logger. The one place console output is allowed; verbosity by level. */

type Level = "debug" | "info" | "warn" | "error";

const ORDER: Record<Level, number> = { debug: 10, info: 20, warn: 30, error: 40 };
const threshold: Level = (import.meta.env.VITE_LOG_LEVEL as Level | undefined) ?? "info";

function emit(level: Level, message: string, fields: Record<string, unknown>): void {
  if (ORDER[level] < ORDER[threshold]) {
    return;
  }
  const line = JSON.stringify({ level, message, ...fields });
  if (level === "warn") {
    console.warn(line);
  } else if (level === "error") {
    console.error(line);
  } else {
    console.info(line);
  }
}

export const logger = {
  debug: (message: string, fields: Record<string, unknown> = {}) => emit("debug", message, fields),
  info: (message: string, fields: Record<string, unknown> = {}) => emit("info", message, fields),
  warn: (message: string, fields: Record<string, unknown> = {}) => emit("warn", message, fields),
  error: (message: string, fields: Record<string, unknown> = {}) => emit("error", message, fields),
};
