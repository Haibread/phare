import type { RecommendationItem } from "../api";
import { posterTint } from "../lib/poster";
import { useTitleDetail } from "../lib/queries";
import { Sheet } from "./Sheet";
import styles from "./components.module.css";

/** "More info" for a recommended title: the card data we already have (poster, fit, why) plus the
 * lazily-fetched synopsis/runtime/links. Opened by tapping a poster card. */
export function TitleDetailSheet({
  item,
  open,
  onOpenChange,
}: {
  item: RecommendationItem;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}): React.JSX.Element {
  const detail = useTitleDetail(item.titleId, open);
  const runtime = detail.data?.runtimeMinutes ?? null;
  const meta = [
    item.year?.toString(),
    runtime ? `${runtime} min` : null,
    item.genres.slice(0, 3).join(", ") || null,
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <Sheet open={open} onOpenChange={onOpenChange} title={item.title}>
      <div className={styles.detail} data-testid="title-detail">
        <div
          className={styles.detailPoster}
          style={item.posterUrl ? undefined : { background: posterTint(item.titleId) }}
        >
          {item.posterUrl && <img src={item.posterUrl} alt="" loading="lazy" />}
        </div>
        <div className={styles.detailBody}>
          {meta && <p className={`muted ${styles.detailMeta}`}>{meta}</p>}
          {item.explanation && (
            <p className={styles.detailWhy}>
              <span className={styles.detailLabel}>Why this</span>
              {item.explanation}
            </p>
          )}
          <div className={styles.detailSynopsis}>
            <span className={styles.detailLabel}>Synopsis</span>
            {detail.isLoading ? (
              <span className="muted">Loading…</span>
            ) : detail.data?.overview ? (
              <p style={{ margin: 0 }}>{detail.data.overview}</p>
            ) : (
              <span className="muted">No synopsis available.</span>
            )}
          </div>
          {(detail.data?.tmdbUrl || detail.data?.imdbUrl) && (
            <div className={styles.detailLinks}>
              {detail.data.tmdbUrl && (
                <a href={detail.data.tmdbUrl} target="_blank" rel="noopener noreferrer">
                  TMDB ↗
                </a>
              )}
              {detail.data.imdbUrl && (
                <a href={detail.data.imdbUrl} target="_blank" rel="noopener noreferrer">
                  IMDb ↗
                </a>
              )}
            </div>
          )}
        </div>
      </div>
    </Sheet>
  );
}
