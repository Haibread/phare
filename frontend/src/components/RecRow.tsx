import type { RecommendationRow } from "../api";
import { PosterCard } from "./PosterCard";
import styles from "./components.module.css";

// "because you watched X" rows carry their seed title id in the key (`because:<uuid>`); pull it out
// so the detail sheet can anchor the "why this" reason on that title. Other rows have no anchor.
const BECAUSE_PREFIX = "because:";
// Rows whose per-item confidence is NOT a taste-fit signal, so showing it as an affinity gauge would
// misread: "continue_watching" is recency warmth, and "popular" is popularity magnitude ("well-known",
// not "fits you") — the same meter meaning two different things is exactly review H8. Both hide it.
const NO_FIT_ROWS = new Set(["continue_watching", "popular"]);

export function RecRow({ row }: { row: RecommendationRow }): React.JSX.Element | null {
  if (row.items.length === 0) {
    return null;
  }
  const anchorTitleId = row.key.startsWith(BECAUSE_PREFIX)
    ? row.key.slice(BECAUSE_PREFIX.length)
    : null;
  const showFit = !NO_FIT_ROWS.has(row.key);
  return (
    <section className={styles.row} data-testid="rec-row" data-row-key={row.key}>
      <div className={styles.rowHead}>
        <h3 className={styles.rowTitle}>{row.title}</h3>
      </div>
      <div className={styles.strip}>
        {row.items.map((item) => (
          <PosterCard
            key={item.titleId}
            item={item}
            showFit={showFit}
            anchorTitleId={anchorTitleId}
          />
        ))}
      </div>
    </section>
  );
}
