import { type Fit, fitFor } from "../lib/fit";
import styles from "./components.module.css";

/** Worded fit label + a coarse 3-segment bar. The bar mirrors the label's bucket exactly — it's a
 * visual echo, not a finer-grained score (no fake precision). */
export function ConfidenceMeter({
  confidence,
  isSwing,
}: {
  confidence: number | null;
  isSwing: boolean;
}): React.JSX.Element {
  const fit: Fit = fitFor(confidence, isSwing);
  return (
    <span className={styles.fit} data-testid="fit">
      <span className={styles.fitLabel} data-tone={fit.tone}>
        {fit.label}
      </span>
      <span className={styles.bar} aria-hidden="true">
        {[1, 2, 3].map((n) => (
          <span key={n} className={styles.seg} data-on={n <= fit.filled} data-tone={fit.tone} />
        ))}
      </span>
    </span>
  );
}
