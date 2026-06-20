import type { RecommendationItem } from "../api";
import { useProfileId } from "../app/ProfileContext";
import { posterTint } from "../lib/poster";
import { useTitleDetail, useTitleExplanation } from "../lib/queries";
import { Sheet } from "./Sheet";
import styles from "./components.module.css";

/** "More info" for a recommended title: the card data we already have (poster, fit) plus the
 * lazily-fetched synopsis/runtime/links and a lazily-generated LLM "why this". Opened by tapping a
 * poster card — nothing here costs an LLM call until the user actually opens it. */
export function TitleDetailSheet({
  item,
  open,
  onOpenChange,
}: {
  item: RecommendationItem;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}): React.JSX.Element {
  const profileId = useProfileId();
  const detail = useTitleDetail(item.titleId, open);
  const explanation = useTitleExplanation(profileId, item.titleId, open);
  // Show the instant template right away; swap to the richer LLM reason once it's generated.
  const why = explanation.data?.explanation ?? item.explanation;
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
          {why && (
            <p className={styles.detailWhy} data-testid="detail-why">
              <span className={styles.detailLabel}>Why this</span>
              {why}
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
