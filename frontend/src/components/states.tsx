import type { TFunction } from "i18next";
import { useTranslation } from "react-i18next";
import styles from "./components.module.css";

/** A human, localised message for any thrown error — network failures (the opaque "Failed to fetch")
 * become a friendly "couldn't reach the server" instead of leaking the raw browser string. Duck-types
 * on ``status === 0`` (an ApiError that never reached the server) rather than importing ApiError, so
 * it stays robust when api is mocked in tests. */
export function errorMessage(error: unknown, t: TFunction): string {
  if (error && typeof error === "object" && (error as { status?: number }).status === 0) {
    return t("networkError");
  }
  return error instanceof Error ? error.message : String(error);
}

export function Loading({ label }: { label?: string }): React.JSX.Element {
  const { t } = useTranslation("common");
  return (
    <output className={styles.center} data-testid="loading">
      <span className={styles.spinner} aria-hidden="true" />
      <span className="faint">{label ?? t("loading")}</span>
    </output>
  );
}

export function RowSkeleton(): React.JSX.Element {
  return (
    <div className={styles.skelStrip} aria-hidden="true">
      {[0, 1, 2, 3, 4].map((n) => (
        <div key={n} className={styles.skel} />
      ))}
    </div>
  );
}

/** A few shimmer lines that hold a card section's space while its query loads, instead of a raw
 * "Loading…" that pops the layout when data arrives (review E3). ``lines`` sizes it to the section. */
export function CardSkeleton({ lines = 3 }: { lines?: number }): React.JSX.Element {
  return (
    <div className={styles.skelLines} aria-hidden="true" data-testid="card-skeleton">
      {Array.from({ length: lines }, (_, i) => i).map((n) => (
        <div
          key={n}
          className={styles.skelLine}
          // Vary widths so it reads as text, not blocks; last line shorter.
          style={{ width: n === lines - 1 ? "60%" : `${90 - n * 8}%` }}
        />
      ))}
    </div>
  );
}

export function ErrorState({
  error,
  onRetry,
}: {
  error: unknown;
  onRetry?: () => void;
}): React.JSX.Element {
  const { t } = useTranslation("common");
  const message = errorMessage(error, t);
  return (
    <div className={styles.center} data-testid="error" role="alert">
      <span className={styles.error}>{t("error", { message })}</span>
      {onRetry && (
        <button type="button" className="btn" onClick={onRetry}>
          {t("tryAgain")}
        </button>
      )}
    </div>
  );
}

export function EmptyState({
  children,
  testid,
}: {
  children: React.ReactNode;
  testid?: string;
}): React.JSX.Element {
  return (
    <div className={styles.center} data-testid={testid}>
      <span className="muted">{children}</span>
    </div>
  );
}
