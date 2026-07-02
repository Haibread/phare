import { useTranslation } from "react-i18next";
import { useEmbeddingsDegraded } from "../lib/degraded";
import { type Fit, fitFor } from "../lib/fit";
import styles from "./components.module.css";

/** Worded fit label + a coarse 3-segment bar. The bar mirrors the label's bucket exactly — it's a
 * visual echo, not a finer-grained score (no fake precision). In offline mode (local hash embedder)
 * the top "strong fit" bucket is suppressed, so an approximate pick can't read as a confident one. */
export function ConfidenceMeter({
  confidence,
  isSwing,
}: {
  confidence: number | null;
  isSwing: boolean;
}): React.JSX.Element {
  const { t } = useTranslation("common");
  const degraded = useEmbeddingsDegraded();
  const fit: Fit = fitFor(confidence, isSwing, degraded);
  return (
    <span className={styles.fit} data-testid="fit">
      <span className={styles.fitLabel} data-tone={fit.tone}>
        {t(fit.labelKey)}
      </span>
      <span className={styles.bar} aria-hidden="true">
        {[1, 2, 3].map((n) => (
          <span key={n} className={styles.seg} data-on={n <= fit.filled} data-tone={fit.tone} />
        ))}
      </span>
    </span>
  );
}
