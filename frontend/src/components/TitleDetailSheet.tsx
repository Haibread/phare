import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { type RecommendationItem, api } from "../api";
import { useProfileId } from "../app/ProfileContext";
import { useEmbeddingsDegraded } from "../lib/degraded";
import { fitFor } from "../lib/fit";
import { posterTint } from "../lib/poster";
import {
  useRowRefreshOnClose,
  useSendTitleFeedback,
  useTitleDetail,
  useUndoChatAction,
} from "../lib/queries";
import { RichText } from "../lib/richText";
import { translateGenre } from "../lib/tasteVocab";
import { TitleAction } from "./Availability";
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
  showFit = true,
}: {
  item: RecommendationItem;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  // Seed of a "because you watched X" row, when opened from one — sharpens the "why this" reason.
  anchorTitleId?: string | null;
  // Whether this title's row carries a taste-fit signal. Mirrors the card's gauge visibility: the
  // sheet is where the *worded* fit label lives now that the card meter dropped its inline text
  // (R6a), so a row without a fit signal (e.g. continue-watching) shows no label here either.
  showFit?: boolean;
}): React.JSX.Element {
  const { t, i18n } = useTranslation("title");
  const { t: tCommon } = useTranslation("common");
  const degraded = useEmbeddingsDegraded();
  const profileId = useProfileId();
  const detail = useTitleDetail(item.titleId, open);
  const [streamedWhy, setStreamedWhy] = useState("");
  const [whyStreaming, setWhyStreaming] = useState(false);

  // "Not interested" is a deliberate, destructive signal — so it lives here in the sheet, behind an
  // explicit tap, not on the card. After sending we hold an in-sheet confirmation with an undo
  // affordance. The recommendation rows must NOT refresh yet: this sheet is mounted inside the
  // row's card, so an immediate refetch would unmount the card and kill the undo mid-offer. The
  // rows refresh when the sheet closes (or unmounts) still-removed; undo cancels the flush and
  // useUndoChatAction's own invalidation restores everything. Reset on close so reopening is clean.
  const feedback = useSendTitleFeedback(profileId);
  const undo = useUndoChatAction(profileId);
  const [removed, setRemoved] = useState(false);
  const [undoToken, setUndoToken] = useState<string | null>(null);
  useRowRefreshOnClose(profileId, open, removed);

  useEffect(() => {
    if (!open) {
      setRemoved(false);
      setUndoToken(null);
    }
  }, [open]);

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
    item.genres
      .slice(0, 3)
      .map((g) => translateGenre(g, i18n.language))
      .join(", ") || null,
  ]
    .filter(Boolean)
    .join(" · ");

  // Rating + credits ride on the lazily-fetched detail (backfilled server-side, so any field may be
  // absent on an old row — the UI omits the corresponding line rather than showing a dash).
  const voteAverage = detail.data?.voteAverage ?? null;
  const voteCount = detail.data?.voteCount ?? null;
  // Below this vote count the score is barely proven — mirror the engine's unproven-cap honesty with
  // a soft "few ratings" hint instead of implying a settled average (matches the reranker's floor).
  const FEW_VOTES = 200;
  const directors = detail.data?.directors ?? [];
  const topCast = detail.data?.topCast ?? [];
  // Locale-aware grouping: 12400 → "12,400" (en) / "12 400" (fr). The score keeps one decimal.
  const numberFmt = new Intl.NumberFormat(i18n.language);
  const rating =
    voteAverage !== null
      ? t("detail.rating", {
          score: new Intl.NumberFormat(i18n.language, {
            minimumFractionDigits: 1,
            maximumFractionDigits: 1,
          }).format(voteAverage),
          count: numberFmt.format(voteCount ?? 0),
        })
      : null;
  const ratingIsThin = voteAverage !== null && (voteCount ?? 0) < FEW_VOTES;

  // Header shows the localized name when one exists — freshest from the detail fetch, else the
  // card's stamped copy, else canonical. Consistent with the (already localized) synopsis below.
  const displayTitle = detail.data?.displayTitle ?? item.displayTitle ?? item.title;

  return (
    <Sheet open={open} onOpenChange={onOpenChange} title={displayTitle}>
      <div className={styles.detail} data-testid="title-detail">
        <div
          className={styles.detailPoster}
          style={item.posterUrl ? undefined : { background: posterTint(item.titleId) }}
        >
          {item.posterUrl && <img src={item.posterUrl} alt="" loading="lazy" />}
        </div>
        <div className={styles.detailBody}>
          {meta && <p className={`muted ${styles.detailMeta}`}>{meta}</p>}
          {rating && (
            // TMDB rating near the year/genre line. No voteAverage → nothing (no dash clutter);
            // a real score on very few votes appends a soft "few ratings" hint (unproven-cap honesty).
            <p className={styles.detailRating} data-testid="detail-rating">
              {rating}
              {ratingIsThin && (
                <span className={styles.detailRatingHint} data-testid="detail-rating-thin">
                  {" "}
                  · {t("detail.ratingFew")}
                </span>
              )}
            </p>
          )}
          {directors.length > 0 && (
            // "Directed by …" / «De …» — comma-joined, one compact ellipsized line (mobile-first).
            <p className={styles.detailCredit} data-testid="detail-directors">
              {t("detail.directedBy", {
                count: directors.length,
                names: directors.join(", "),
              })}
            </p>
          )}
          {topCast.length > 0 && (
            // "With A, B, C" / «Avec …» — one compact ellipsized line.
            <p className={styles.detailCredit} data-testid="detail-cast">
              {t("detail.cast", { names: topCast.join(", ") })}
            </p>
          )}
          {showFit && (
            // The worded fit lives here now that the card gauge dropped its inline text (R6a). Tone
            // mirrors the gauge so a strong fit reads saffron; swings keep their categorical wording.
            <p
              className={styles.detailFit}
              data-testid="detail-fit"
              data-tone={fitFor(item.confidence, item.isSwing, degraded).tone}
            >
              {tCommon(fitFor(item.confidence, item.isSwing, degraded).labelKey)}
            </p>
          )}
          {why && (
            <p className={styles.detailWhy} data-testid="detail-why">
              <span className={styles.detailLabel}>{t("detail.whyThis")}</span>
              <RichText text={why} />
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
          {/* Request / availability — the constructive action, so it sits above the destructive
              "not interested". Renders nothing when there's no availability provider in the tree
              (Browse/chat) or the library integration is unconfigured, exactly as it did on the
              card — a card's request button was only ever wired up under Search's provider. */}
          <div className={styles.detailAction} data-testid="detail-action">
            <TitleAction titleId={item.titleId} />
          </div>
          {/* Secondary, deliberate "not interested" — full-width but visually quiet, kept apart from
              the primary content. After the tap it flips to a confirmation + undo in place. */}
          <div className={styles.detailFeedback}>
            {removed ? (
              <div className={styles.detailRemoved} data-testid="detail-removed">
                <span className="muted">{t("feedback.removed")}</span>
                <button
                  type="button"
                  className={styles.undoInline}
                  data-testid="detail-undo"
                  disabled={!undoToken || undo.isPending}
                  onClick={restore}
                >
                  {t("feedback.undo")}
                </button>
              </div>
            ) : (
              <button
                type="button"
                className={`btn ${styles.detailDismiss}`}
                data-testid="detail-not-interested"
                disabled={feedback.isPending}
                onClick={dismiss}
              >
                {t("feedback.notInterested")}
              </button>
            )}
          </div>
        </div>
      </div>
    </Sheet>
  );
}
