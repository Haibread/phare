import { useId, useState } from "react";
import { useTranslation } from "react-i18next";
import type { RecommendationItem } from "../api";
import { useEmbeddingsDegraded } from "../lib/degraded";
import { fitFor } from "../lib/fit";
import { posterTint } from "../lib/poster";
import { translateGenre } from "../lib/tasteVocab";
import { ConfidenceMeter } from "./ConfidenceMeter";
import { TitleDetailSheet } from "./TitleDetailSheet";
import styles from "./components.module.css";

export function PosterCard({
  item,
  showFit = true,
  hideNullFit = false,
  showRating = false,
  anchorTitleId = null,
}: {
  item: RecommendationItem;
  showFit?: boolean;
  // When true, a `null` confidence suppresses the fit gauge/label entirely (rather than the neutral
  // "worth a look" sliver). Search sets this so cards only show a gauge once the backend sends a real
  // confidence; home rows keep the default. See ConfidenceMeter for the rationale.
  hideNullFit?: boolean;
  // Search cards show a compact TMDB rating ("★ 8.4 · 37k") so junk is tellable from the real film.
  // Home rows keep the default (off): they carry the taste-fit gauge instead — one signal per row.
  showRating?: boolean;
  // Seed title of a "because you watched X" row, when this card lives in one — passed to the detail
  // sheet so the "why this" reason can open from that concrete link.
  anchorTitleId?: string | null;
}): React.JSX.Element {
  const { t, i18n } = useTranslation("title");
  const { t: tCommon } = useTranslation("common");
  const degraded = useEmbeddingsDegraded();
  const swingHelpId = useId(); // unique per card so each swing badge describes its own help text
  const [imgFailed, setImgFailed] = useState(false);
  const [detailOpen, setDetailOpen] = useState(false);
  const showPoster = item.posterUrl !== null && !imgFailed;
  // Localized display name when the backend has one cached ("Amour éternel"), canonical otherwise.
  const displayTitle = item.displayTitle ?? item.title;
  // Effective fit visibility: a card that's told to show fit but has no confidence signal (Search
  // against an older backend / a profile without taste) reads as "no fit" everywhere — gauge, aria
  // name, and detail label — so the three stay consistent.
  const fitVisible = showFit && !(hideNullFit && item.confidence === null && !item.isSwing);

  // Compact locale-aware rating for search cards: score keeps one decimal, the vote count compacts
  // (37000 → "37K" en / "37 k" fr). Same star convention as the detail sheet's fuller rating line.
  const rating =
    showRating && item.voteAverage !== null
      ? t(item.voteCount !== null ? "card.rating" : "card.ratingNoCount", {
          score: new Intl.NumberFormat(i18n.language, {
            minimumFractionDigits: 1,
            maximumFractionDigits: 1,
          }).format(item.voteAverage),
          count:
            item.voteCount !== null
              ? new Intl.NumberFormat(i18n.language, {
                  notation: "compact",
                  maximumFractionDigits: 0,
                }).format(item.voteCount)
              : "",
        })
      : null;

  // A card has exactly one action: open the detail sheet. The destructive "not interested" signal
  // (round 3) and the request/availability action (round 4) both moved into the sheet, off the
  // card's primary click target where a stray tap was too easy — especially on touch.
  return (
    <article className={styles.card} data-testid="rec-card">
      {/* Poster + title open the detail sheet; the request button below stays separately clickable.
          An explicit aria-label gives the button one clean name — "Open {title} ({year}), {fit}" —
          instead of the screen reader stitching together the badges, title and meta text (review L2). */}
      <button
        type="button"
        className={styles.cardOpen}
        data-testid="rec-card-open"
        aria-label={
          fitVisible
            ? t("openCard", {
                title: displayTitle,
                year: item.year !== null ? ` (${item.year})` : "",
                fit: tCommon(fitFor(item.confidence, item.isSwing, degraded).labelKey),
              })
            : t("openCardNoFit", {
                title: displayTitle,
                year: item.year !== null ? ` (${item.year})` : "",
              })
        }
        onClick={() => setDetailOpen(true)}
      >
        <div
          className={`${styles.poster} ${showPoster ? "" : styles.posterEmpty}`}
          style={showPoster ? undefined : { background: posterTint(item.titleId) }}
        >
          {item.isSwing && (
            <>
              {/* The badge is unlabelled without this: a "swing" means nothing until you're told it's
                  a deliberate stretch. `title` gives the hover tooltip, aria-describedby the a11y one. */}
              <span
                className={styles.swingBadge}
                data-testid="swing-badge"
                title={t("badge.swingHelp")}
                aria-describedby={swingHelpId}
              >
                {t("badge.swing")}
              </span>
              <span id={swingHelpId} className="sr-only" data-testid="swing-help">
                {t("badge.swingHelp")}
              </span>
            </>
          )}
          {item.watched && (
            <span className={styles.watchedBadge} data-testid="watched-badge">
              {t("badge.watched")}
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
            // Honest "no cover art" placeholder: the title + year centered over the tinted tile,
            // instead of a blank grey box or the raw title caption doubled with the label below
            // (round 7, fixes 4 & 6).
            <span className={styles.posterFallback} data-testid="poster-fallback">
              <span className={styles.posterFallbackTitle}>{displayTitle}</span>
              {item.year !== null && <span className={styles.posterFallbackYear}>{item.year}</span>}
            </span>
          )}
        </div>
        <div className={styles.cardTitle}>{displayTitle}</div>
        <div className={styles.cardMeta}>
          {item.year ?? "—"}
          {item.genres[0] !== undefined && ` · ${translateGenre(item.genres[0], i18n.language)}`}
        </div>
        {rating !== null && (
          // No voteAverage → no line at all (an unrated row shows nothing, not a dash).
          <div className={styles.cardRating} data-testid="card-rating">
            {rating}
          </div>
        )}
      </button>
      {showFit && (
        <ConfidenceMeter
          confidence={item.confidence}
          isSwing={item.isSwing}
          hideNullFit={hideNullFit}
        />
      )}
      <TitleDetailSheet
        item={item}
        open={detailOpen}
        onOpenChange={setDetailOpen}
        anchorTitleId={anchorTitleId}
        showFit={fitVisible}
      />
    </article>
  );
}
