import styles from "./components.module.css";

export function Loading({ label = "Loading…" }: { label?: string }): React.JSX.Element {
  return (
    <output className={styles.center} data-testid="loading">
      <span className={styles.spinner} aria-hidden="true" />
      <span className="faint">{label}</span>
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

export function ErrorState({
  error,
  onRetry,
}: {
  error: unknown;
  onRetry?: () => void;
}): React.JSX.Element {
  const message = error instanceof Error ? error.message : String(error);
  return (
    <div className={styles.center} data-testid="error" role="alert">
      <span className={styles.error}>Something went wrong: {message}</span>
      {onRetry && (
        <button type="button" className="btn" onClick={onRetry}>
          Try again
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
