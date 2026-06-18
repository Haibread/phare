import { useState } from "react";
import type { RecommendationItem } from "../api";
import { ConfidenceMeter } from "./ConfidenceMeter";
import styles from "./components.module.css";

/** Deterministic tint from the title id, so the text-fallback poster looks intentional and
 * stable across renders. Real artwork (posterUrl) lands in a later PR; until then this. */
function tint(seed: string): string {
  let hash = 0;
  for (let i = 0; i < seed.length; i++) {
    hash = (hash * 31 + seed.charCodeAt(i)) % 360;
  }
  return `hsl(${hash} 38% 32%)`;
}

export function PosterCard({ item }: { item: RecommendationItem }): React.JSX.Element {
  const [imgFailed, setImgFailed] = useState(false);
  const showPoster = item.posterUrl !== null && !imgFailed;
  return (
    <article className={styles.card} data-testid="rec-card">
      <div
        className={styles.poster}
        style={showPoster ? undefined : { background: tint(item.titleId) }}
      >
        {item.isSwing && (
          <span className={styles.swingBadge} data-testid="swing-badge">
            swing
          </span>
        )}
        {showPoster ? (
          <img
            src={item.posterUrl ?? ""}
            alt=""
            loading="lazy"
            onError={() => setImgFailed(true)}
          />
        ) : (
          <span className={styles.posterFallback}>{item.title}</span>
        )}
      </div>
      <div className={styles.cardTitle} title={item.explanation ?? undefined}>
        {item.title}
      </div>
      <div className={styles.cardMeta}>
        {item.year ?? "—"}
        {item.genres.length > 0 && ` · ${item.genres[0]}`}
      </div>
      <ConfidenceMeter confidence={item.confidence} isSwing={item.isSwing} />
    </article>
  );
}
