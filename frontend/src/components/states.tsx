import { useTranslation } from "react-i18next";
import styles from "./components.module.css";

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

export function ErrorState({
  error,
  onRetry,
}: {
  error: unknown;
  onRetry?: () => void;
}): React.JSX.Element {
  const { t } = useTranslation("common");
  const message = error instanceof Error ? error.message : String(error);
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
