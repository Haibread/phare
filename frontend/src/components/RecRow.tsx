import type { RecommendationRow } from "../api";
import { PosterCard } from "./PosterCard";
import styles from "./components.module.css";

export function RecRow({ row }: { row: RecommendationRow }): React.JSX.Element | null {
  if (row.items.length === 0) {
    return null;
  }
  return (
    <section className={styles.row} data-testid="rec-row" data-row-key={row.key}>
      <div className={styles.rowHead}>
        <h3 className={styles.rowTitle}>{row.title}</h3>
      </div>
      <div className={styles.strip}>
        {row.items.map((item) => (
          <PosterCard key={item.titleId} item={item} />
        ))}
      </div>
    </section>
  );
}
