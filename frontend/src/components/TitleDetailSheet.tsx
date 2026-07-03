import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { type RecommendationItem, api } from "../api";
import { useProfileId } from "../app/ProfileContext";
import { posterTint } from "../lib/poster";
import { useTitleDetail } from "../lib/queries";
import { Sheet } from "./Sheet";
import styles from "./components.module.css";

/** "More info" for a recommended title: the card data we already have (poster, fit) plus the
 * lazily-fetched synopsis/runtime/links and a lazily-*streamed* LLM "why this". Opened by tapping a
 * poster card — nothing here costs an LLM call until the user actually opens it. */
export function TitleDetailSheet({
  item,
  open,
  onOpenChange,
  anchorTitleId = null,
}: {
  item: RecommendationItem;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  // Seed of a "because you watched X" row, when opened from one — sharpens the "why this" reason.
  anchorTitleId?: string | null;
}): React.JSX.Element {
  const { t } = useTranslation("title");
  const profileId = useProfileId();
  const detail = useTitleDetail(item.titleId, open);
  const [streamedWhy, setStreamedWhy] = useState("");
  const [whyStreaming, setWhyStreaming] = useState(false);

  // Generate the LLM "why this" only while the sheet is open, streaming it in; abort on close.
  useEffect(() => {
    if (!open) {
      setStreamedWhy("");
      setWhyStreaming(false);
      return;
    }
    const controller = new AbortController();
    setStreamedWhy("");
    setWhyStreaming(true);
    api
      .streamTitleExplanation(
        profileId,
        item.titleId,
        {
          onDelta: (chunk) => setStreamedWhy((prev) => prev + chunk),
          onDone: () => setWhyStreaming(false),
        },
        controller.signal,
        anchorTitleId,
      )
      .catch(() => setWhyStreaming(false)); // aborted on close, or a transient error
    return () => controller.abort();
  }, [open, profileId, item.titleId, anchorTitleId]);

  // Show the instant template until the streamed reason starts arriving, then the richer one.
  const why = streamedWhy || item.explanation;
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
              <span className={styles.detailLabel}>{t("detail.whyThis")}</span>
              {why}
              {whyStreaming && <span className={styles.whyCaret} aria-hidden="true" />}
            </p>
          )}
          <div className={styles.detailSynopsis}>
            <span className={styles.detailLabel}>{t("detail.synopsis")}</span>
            {detail.isLoading ? (
              <span className="muted">{t("detail.loading")}</span>
            ) : detail.data?.overview ? (
              <p style={{ margin: 0 }}>{detail.data.overview}</p>
            ) : (
              <span className="muted">{t("detail.noSynopsis")}</span>
            )}
          </div>
          {(detail.data?.tmdbUrl || detail.data?.imdbUrl) && (
            <div className={styles.detailLinks}>
              {detail.data.tmdbUrl && (
                <a
                  href={detail.data.tmdbUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                  aria-label={t("detail.externalLink", { name: "TMDB" })}
                >
                  TMDB ↗
                </a>
              )}
              {detail.data.imdbUrl && (
                <a
                  href={detail.data.imdbUrl}
                  target="_blank"
                  rel="noopener noreferrer"
                  aria-label={t("detail.externalLink", { name: "IMDb" })}
                >
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
