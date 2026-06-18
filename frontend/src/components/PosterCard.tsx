import { useState } from "react";
import type { RecommendationItem } from "../api";
import { posterTint } from "../lib/poster";
import { TitleAction } from "./Availability";
import { ConfidenceMeter } from "./ConfidenceMeter";
import styles from "./components.module.css";

export function PosterCard({
  item,
  showFit = true,
}: {
  item: RecommendationItem;
  showFit?: boolean;
}): React.JSX.Element {
  const [imgFailed, setImgFailed] = useState(false);
  const showPoster = item.posterUrl !== null && !imgFailed;
  return (
    <article className={styles.card} data-testid="rec-card">
      <div
        className={styles.poster}
        style={showPoster ? undefined : { background: posterTint(item.titleId) }}
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
      {showFit && <ConfidenceMeter confidence={item.confidence} isSwing={item.isSwing} />}
      <TitleAction titleId={item.titleId} />
    </article>
  );
}
