import { useTranslation } from "react-i18next";
import { useEmbeddingsDegraded } from "../lib/degraded";
import { type Fit, fitFor } from "../lib/fit";
import styles from "./components.module.css";

/** A slim continuous-fill gauge whose width is proportional to the confidence and whose colour is
 * keyed to the fit bucket (success/saffron for a strong fit, neutral otherwise). No number and no
 * inline text — a percentage would be false precision on a heuristic blend (owner request R6a). The
 * bucket wording still reaches assistive tech via the gauge's ``aria-label`` + hover ``title``, and
 * it lives visibly in the detail sheet.
 *
 * Swings render nothing here: a swing is a categorical "deliberate stretch", not a point on the same
 * scalar axis, so the card shows its swing badge instead of a gauge. In offline mode (local hash
 * embedder) ``fitFor`` caps the tone so an approximate pick can never read as a confident success. */
export function ConfidenceMeter({
  confidence,
  isSwing,
  hideNullFit = false,
}: {
  confidence: number | null;
  isSwing: boolean;
  // When true, a `null` confidence renders no gauge at all instead of the neutral "worth a look"
  // sliver. Search sets this: a card only earns a gauge once the profile has a taste + embeddings
  // and the backend sends a real confidence; against an older backend (confidence still null) the
  // gauge stays absent. Home rows keep the default (false) — their null-confidence sliver is a
  // deliberate "worth a look" affordance, a different state from "no signal".
  hideNullFit?: boolean;
}): React.JSX.Element | null {
  const { t } = useTranslation("common");
  const degraded = useEmbeddingsDegraded();
  if (hideNullFit && confidence === null && !isSwing) {
    return null;
  }
  const fit: Fit = fitFor(confidence, isSwing, degraded);
  if (fit.fill === null) {
    // Swing — the poster card carries the categorical badge; no scalar gauge.
    return null;
  }
  const label = t(fit.labelKey);
  const pct = `${Math.round(fit.fill * 100)}%`;
  return (
    <span
      className={styles.fit}
      data-testid="fit"
      role="meter"
      aria-label={label}
      title={label}
      data-tone={fit.tone}
    >
      <span className={styles.gaugeTrack} aria-hidden="true">
        <span className={styles.gaugeFill} data-tone={fit.tone} style={{ width: pct }} />
      </span>
    </span>
  );
}
