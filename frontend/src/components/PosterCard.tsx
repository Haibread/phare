import { useId, useState } from "react";
import { useTranslation } from "react-i18next";
import type { RecommendationItem } from "../api";
import { useProfileId } from "../app/ProfileContext";
import { posterTint } from "../lib/poster";
import { useSendTitleFeedback, useUndoChatAction } from "../lib/queries";
import { TitleAction } from "./Availability";
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
  const { t } = useTranslation("title");
  const profileId = useProfileId();
  const swingHelpId = useId(); // unique per card so each swing badge describes its own help text
  const [imgFailed, setImgFailed] = useState(false);
  const [detailOpen, setDetailOpen] = useState(false);
  // Local removal: the card leaves its row on "not interested" but stays in place as an undo slot,
  // so we don't refetch the whole row out from under the user (see useSendTitleFeedback).
  const [removed, setRemoved] = useState(false);
  const [undoToken, setUndoToken] = useState<string | null>(null);
  const feedback = useSendTitleFeedback(profileId);
  const undo = useUndoChatAction(profileId);
  const showPoster = item.posterUrl !== null && !imgFailed;

  function dismiss(): void {
    setRemoved(true); // optimistic — revert if the write fails
    feedback.mutate(
      { titleId: item.titleId, signal: "not_interested" },
      {
        onSuccess: (res) => setUndoToken(res.undoToken),
        onError: () => setRemoved(false),
      },
    );
  }

  function restore(): void {
    if (!undoToken) return;
    undo.mutate(undoToken, {
      onSuccess: () => {
        setRemoved(false);
        setUndoToken(null);
      },
    });
  }

  if (removed) {
    return (
      <article className={styles.card} data-testid="rec-card">
        <div className={styles.cardRemoved} data-testid="rec-card-removed">
          <span>{t("feedback.removed")}</span>
          <button
            type="button"
            className={styles.undoInline}
            data-testid="undo-not-interested"
            disabled={!undoToken || undo.isPending}
            onClick={restore}
          >
            {t("feedback.undo")}
          </button>
        </div>
      </article>
    );
  }

  return (
    <article className={styles.card} data-testid="rec-card">
      <button
        type="button"
        className={styles.dismiss}
        data-testid="not-interested"
        aria-label={t("feedback.notInterested")}
        title={t("feedback.notInterested")}
        disabled={feedback.isPending}
        onClick={dismiss}
      >
        ✕
      </button>
      {/* Poster + title open the detail sheet; the request button below stays separately clickable. */}
      <button
        type="button"
        className={styles.cardOpen}
        data-testid="rec-card-open"
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
          {item.genres.length > 0 && ` · ${item.genres[0]}`}
        </div>
      </button>
      {showFit && <ConfidenceMeter confidence={item.confidence} isSwing={item.isSwing} />}
      <TitleAction titleId={item.titleId} />
      <TitleDetailSheet
        item={item}
        open={detailOpen}
        onOpenChange={setDetailOpen}
        anchorTitleId={anchorTitleId}
      />
    </article>
  );
}
