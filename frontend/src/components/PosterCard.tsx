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
  anchorTitleId = null,
}: {
  item: RecommendationItem;
  showFit?: boolean;
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
          showFit
            ? t("openCard", {
                title: item.title,
                year: item.year !== null ? ` (${item.year})` : "",
                fit: tCommon(fitFor(item.confidence, item.isSwing, degraded).labelKey),
              })
            : t("openCardNoFit", {
                title: item.title,
                year: item.year !== null ? ` (${item.year})` : "",
              })
        }
        onClick={() => setDetailOpen(true)}
      >
        <div
          className={styles.poster}
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
            <span className={styles.posterFallback}>{item.title}</span>
          )}
        </div>
        <div className={styles.cardTitle}>{item.title}</div>
        <div className={styles.cardMeta}>
          {item.year ?? "—"}
          {item.genres[0] !== undefined && ` · ${translateGenre(item.genres[0], i18n.language)}`}
        </div>
      </button>
      {showFit && <ConfidenceMeter confidence={item.confidence} isSwing={item.isSwing} />}
      <TitleDetailSheet
        item={item}
        open={detailOpen}
        onOpenChange={setDetailOpen}
        anchorTitleId={anchorTitleId}
      />
    </article>
  );
}
