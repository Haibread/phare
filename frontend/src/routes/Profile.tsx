import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { type HistoryItem, isLLMUnavailable, isNotFound } from "../api";
import { useProfileId } from "../app/ProfileContext";
import { EditableChips } from "../components/EditableChips";
import { CardSkeleton, ErrorState, errorMessage } from "../components/states";
import {
  keys,
  useAddMemoryNote,
  useCommitments,
  useConnectedSources,
  useConversion,
  useDeleteMemoryNote,
  useGenerateTaste,
  useHistory,
  useMemory,
  useTaste,
  useTasteFacets,
  useUpdateTaste,
} from "../lib/queries";
import { posterTint } from "../lib/poster";
import { RichText } from "../lib/richText";
import { translateFacetLabel } from "../lib/tasteVocab";
import { relativeTime, relativeTimeLocalized } from "../lib/time";
import { SourcePicker } from "../onboarding/SourcePicker";
import { AccountCard } from "./AccountCard";
import { Attributions } from "./Attributions";
import styles from "./routes.module.css";

const SOURCE_LABELS: Record<string, string> = {
  trakt: "Trakt",
  plex: "Plex",
  jellyfin: "Jellyfin",
  seerr: "Seerr",
};

function stringList(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : [];
}

function episodeLabel(item: HistoryItem): string {
  if (item.seasonNumber !== null && item.episodeNumber !== null) {
    return ` S${item.seasonNumber}E${item.episodeNumber}`;
  }
  return "";
}

export function Profile(): React.JSX.Element {
  const { t, i18n } = useTranslation("profile");
  const { t: tCommon } = useTranslation("common");
  const profileId = useProfileId();
  const qc = useQueryClient();
  const taste = useTaste(profileId);
  const facets = useTasteFacets(profileId);
  const history = useHistory(profileId);
  const conversion = useConversion(profileId);
  const sources = useConnectedSources(profileId);
  const updateTaste = useUpdateTaste(profileId);
  const generateTaste = useGenerateTaste(profileId);
  const commitments = useCommitments(profileId);
  const memory = useMemory(profileId);
  const addNote = useAddMemoryNote(profileId);
  const deleteNote = useDeleteMemoryNote(profileId);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [noteDraft, setNoteDraft] = useState("");
  // Infinite history pages flattened for the table; total comes with every page.
  const historyItems = history.data?.pages.flatMap((p) => p.items) ?? [];
  const historyTotal = history.data?.pages[0]?.total ?? 0;

  function submitNote() {
    const text = noteDraft.trim();
    if (text === "") {
      return;
    }
    addNote.mutate({ text, kind: "preference" });
    setNoteDraft("");
  }

  const likes = stringList(taste.data?.structured.likes);
  const avoids = stringList(taste.data?.structured.hard_avoids);
  const conv = conversion.data;

  // Edits persist as taste overrides (overrides win per-key and survive auto-regeneration).
  function setOverride(key: "likes" | "hard_avoids", list: string[]) {
    const current = (taste.data?.userOverrides ?? {}) as Record<string, unknown>;
    updateTaste.mutate({ ...current, [key]: list });
  }

  return (
    <div className={styles.page} data-testid="profile">
      <h1 className={styles.pageTitle}>{t("title")}</h1>

      {/* Taste --------------------------------------------------------- */}
      <section className={styles.card} data-testid="taste-card">
        <div className={styles.cardHead}>
          <h2 style={{ fontSize: "1.05rem" }}>{t("taste.heading")}</h2>
          <span className="faint" style={{ fontSize: "0.75rem" }}>
            {t("taste.updatesAuto")}
          </span>
        </div>

        {/* Freshness evidence for the "updates automatically" claim above. Null generatedAt means
            the taste was never generated (fresh profile) — show nothing rather than a bogus time. */}
        {taste.data?.generatedAt && (
          <p
            className="faint"
            data-testid="taste-freshness"
            style={{ fontSize: "0.75rem", marginTop: "calc(-1 * var(--sp-2))" }}
          >
            {t("taste.updatedAt", {
              when: relativeTimeLocalized(taste.data.generatedAt, i18n.language),
            })}
          </p>
        )}

        {taste.isPending ? (
          <CardSkeleton lines={4} />
        ) : taste.isError && !isNotFound(taste.error) ? (
          // A real failure (500, network) keeps the alarming error + retry. A 404 is just "no taste
          // yet" on a fresh profile — a first visit, not an error — so it falls through to the
          // friendly empty copy below, with the single "Generate now" CTA (rendered further down).
          <ErrorState error={taste.error} onRetry={() => taste.refetch()} />
        ) : taste.data?.summary ? (
          <>
            <p className="muted" data-testid="taste-summary">
              <RichText text={taste.data.summary} />
            </p>
            <div style={{ marginTop: "var(--sp-3)" }}>
              <EditableChips
                label={t("taste.drawnTo")}
                tone="like"
                items={likes}
                busy={updateTaste.isPending}
                onAdd={(v) => setOverride("likes", [...likes, v])}
                onRemove={(v) =>
                  setOverride(
                    "likes",
                    likes.filter((x) => x !== v),
                  )
                }
              />
            </div>
            <EditableChips
              label={t("taste.avoiding")}
              tone="avoid"
              items={avoids}
              busy={updateTaste.isPending}
              onAdd={(v) => setOverride("hard_avoids", [...avoids, v])}
              onRemove={(v) =>
                setOverride(
                  "hard_avoids",
                  avoids.filter((x) => x !== v),
                )
              }
            />
            {taste.data.confidence !== null && (
              <div className={styles.meterTrack} title={`confidence ${taste.data.confidence}`}>
                <div
                  className={styles.meterFill}
                  style={{ width: `${Math.round(taste.data.confidence * 100)}%` }}
                />
              </div>
            )}
          </>
        ) : (
          <p className="muted" data-testid="taste-empty">
            {/* 404 = fresh profile, no taste generated yet: steer to the generate CTA. Any other
                falsy state (loaded but summary still null) keeps the "builds automatically" note. */}
            {taste.isError ? t("taste.noTasteYet") : t("taste.empty")}
          </p>
        )}

        {/* Always offer (re)generation — taste already built? let the user recompute it. */}
        {!taste.isPending && (
          <div style={{ marginTop: "var(--sp-3)" }}>
            <button
              type="button"
              className="btn btn-primary"
              data-testid="taste-generate"
              onClick={() => generateTaste.mutate()}
              disabled={generateTaste.isPending}
            >
              {generateTaste.isPending
                ? t("taste.generating")
                : taste.data?.summary
                  ? t("taste.regenerate")
                  : t("taste.generate")}
            </button>
            {generateTaste.isError && (
              <p className={styles.errorText} data-testid="taste-generate-error" role="alert">
                {isLLMUnavailable(generateTaste.error)
                  ? t("taste.generateUnavailable")
                  : t("taste.generateError", {
                      message: errorMessage(generateTaste.error, tCommon),
                    })}
              </p>
            )}
          </div>
        )}
      </section>

      {/* Taste facets ---------------------------------------------------
          The distinct taste modes the recommender actually retrieves for (round 10), surfaced so
          the taste stays inspectable. Hidden entirely when the backend returns none — a single-mode
          taste (or a fresh profile) has no split worth showing. Errors hide it too: this is an
          insight panel, not a critical path, so it degrades to absence rather than an alarm. */}
      {facets.data && facets.data.facets.length > 0 && (
        <section className={styles.card} data-testid="taste-facets">
          <h2 style={{ fontSize: "1.05rem" }}>{t("facets.heading")}</h2>
          <p className="faint" style={{ fontSize: "0.78rem", marginBottom: "var(--sp-2)" }}>
            {t("facets.subtitle")}
          </p>
          {facets.data.facets.map((facet) => (
            <div
              key={facet.exemplars[0]?.titleId ?? facet.label}
              className={styles.facetRow}
              data-testid="facet-row"
            >
              <div className={styles.facetInfo}>
                <div className={styles.facetLabelLine}>
                  <span className={styles.facetLabel} data-testid="facet-label">
                    {translateFacetLabel(facet.label, i18n.language)}
                  </span>
                  <span className="faint" data-testid="facet-share">
                    {t("facets.share", {
                      pct: Math.round(facet.weight * 100),
                      count: facet.titleCount,
                    })}
                  </span>
                </div>
                <div
                  className={styles.meterTrack}
                  role="meter"
                  aria-valuenow={Math.round(facet.weight * 100)}
                  aria-valuemin={0}
                  aria-valuemax={100}
                  aria-label={t("facets.weightLabel", {
                    label: translateFacetLabel(facet.label, i18n.language),
                  })}
                >
                  <div
                    className={styles.meterFill}
                    style={{ width: `${Math.round(facet.weight * 100)}%` }}
                  />
                </div>
              </div>
              <div className={styles.facetPosters}>
                {facet.exemplars.map((exemplar) =>
                  exemplar.posterUrl !== null ? (
                    <img
                      key={exemplar.titleId}
                      className={styles.facetPoster}
                      src={exemplar.posterUrl}
                      alt={exemplar.title}
                      title={exemplar.title}
                      loading="lazy"
                    />
                  ) : (
                    // No artwork: the deterministic tinted tile the poster cards use, with the
                    // title as tooltip — never a broken-image icon.
                    <span
                      key={exemplar.titleId}
                      className={styles.facetPoster}
                      style={{ background: posterTint(exemplar.titleId) }}
                      title={exemplar.title}
                      aria-label={exemplar.title}
                    />
                  ),
                )}
              </div>
            </div>
          ))}
        </section>
      )}

      {/* Sources ------------------------------------------------------- */}
      <section className={styles.card}>
        <div className={styles.cardHead}>
          <h2 style={{ fontSize: "1.05rem" }}>{t("sources.heading")}</h2>
          <button
            type="button"
            className="btn"
            data-testid="add-source"
            onClick={() => setPickerOpen(true)}
          >
            {t("sources.add")}
          </button>
        </div>
        {sources.isPending ? (
          <CardSkeleton lines={2} />
        ) : sources.isError ? (
          <ErrorState error={sources.error} onRetry={() => sources.refetch()} />
        ) : sources.data && sources.data.length > 0 ? (
          <div data-testid="connected-sources">
            {sources.data.map((s) => (
              <div className={styles.sourceLine} key={s.source} data-testid="connected-source">
                <span>{SOURCE_LABELS[s.source] ?? s.source}</span>
                <span className="faint" style={{ marginLeft: "auto", fontSize: "0.8rem" }}>
                  {s.lastSyncedAt
                    ? t("sources.synced", { when: relativeTime(s.lastSyncedAt) })
                    : t("sources.connected")}
                </span>
              </div>
            ))}
          </div>
        ) : (
          <p className="muted" style={{ fontSize: "0.88rem" }} data-testid="no-sources">
            {t("sources.empty")}
          </p>
        )}
        {conv && (
          <p className="faint" data-testid="conversion" style={{ fontSize: "0.82rem" }}>
            {conv.rate === null
              ? t("sources.conversionPending", { topK: conv.topK, withinDays: conv.withinDays })
              : t("sources.conversion", {
                  rate: Math.round(conv.rate * 100),
                  shown: conv.shown,
                })}
          </p>
        )}
      </section>

      {/* Watch plans (commitments) ------------------------------------- */}
      <section className={styles.card} data-testid="watch-plans">
        <h2 style={{ fontSize: "1.05rem", marginBottom: "var(--sp-3)" }}>{t("plans.heading")}</h2>
        {commitments.isPending ? (
          <CardSkeleton lines={2} />
        ) : commitments.isError ? (
          <ErrorState error={commitments.error} onRetry={() => commitments.refetch()} />
        ) : commitments.data && commitments.data.items.length > 0 ? (
          <div>
            {commitments.data.items.map((c) => (
              <div className={styles.sourceLine} key={c.id} data-testid="commitment">
                <span>{c.title}</span>
                <span className="faint" style={{ marginLeft: "auto", fontSize: "0.8rem" }}>
                  {c.status === "pending" ? t("plans.toWatch") : c.status}
                </span>
              </div>
            ))}
          </div>
        ) : (
          <p className="muted" style={{ fontSize: "0.88rem" }} data-testid="no-plans">
            {t("plans.empty")}
          </p>
        )}
      </section>

      {/* Memory (generalist notes) ------------------------------------- */}
      <section className={styles.card} data-testid="memory-card">
        <div className={styles.cardHead}>
          <h2 style={{ fontSize: "1.05rem" }}>{t("memory.heading")}</h2>
          <span className="faint" style={{ fontSize: "0.75rem" }}>
            {t("memory.subtitle")}
          </span>
        </div>
        <div style={{ display: "flex", gap: "var(--sp-2)" }}>
          <input
            className="field"
            data-testid="memory-input"
            aria-label={t("memory.inputLabel")}
            placeholder={t("memory.inputPlaceholder")}
            value={noteDraft}
            onChange={(e) => setNoteDraft(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && submitNote()}
          />
          <button
            type="button"
            className="btn"
            data-testid="memory-add"
            onClick={submitNote}
            disabled={addNote.isPending || noteDraft.trim() === ""}
          >
            {t("memory.remember")}
          </button>
        </div>
        {memory.isError ? (
          <div style={{ marginTop: "var(--sp-3)" }}>
            <ErrorState error={memory.error} onRetry={() => memory.refetch()} />
          </div>
        ) : memory.data && memory.data.items.length > 0 ? (
          <div style={{ marginTop: "var(--sp-3)" }}>
            {memory.data.items.map((n) => (
              <div className={styles.sourceLine} key={n.id} data-testid="memory-note">
                <span>{n.text}</span>
                <button
                  type="button"
                  className={styles.chipRemove}
                  aria-label={t("memory.forget", { text: n.text })}
                  data-testid="memory-forget"
                  onClick={() => deleteNote.mutate(n.id)}
                >
                  ×
                </button>
              </div>
            ))}
          </div>
        ) : (
          <p className="muted" style={{ fontSize: "0.88rem", marginTop: "var(--sp-2)" }}>
            {t("memory.empty")}
          </p>
        )}
      </section>

      {/* History ------------------------------------------------------- */}
      <section className={styles.card}>
        <h2 style={{ fontSize: "1.05rem", marginBottom: "var(--sp-3)" }}>{t("history.heading")}</h2>
        {history.isPending ? (
          <CardSkeleton lines={4} />
        ) : history.isError ? (
          <ErrorState error={history.error} onRetry={() => history.refetch()} />
        ) : historyItems.length === 0 ? (
          <p className="muted">{t("history.empty")}</p>
        ) : (
          <>
            <table className={styles.histTable} data-testid="history-table">
              <thead>
                <tr>
                  <th>{t("history.columns.title")}</th>
                  <th>{t("history.columns.event")}</th>
                  <th>{t("history.columns.rating")}</th>
                  <th>{t("history.columns.when")}</th>
                </tr>
              </thead>
              <tbody>
                {historyItems.map((item) => (
                  <tr key={item.id} data-testid="history-row">
                    <td>
                      {item.title}
                      {episodeLabel(item)}
                    </td>
                    {/* Localize known event enums; fall back to the raw backend string for any
                        member the map doesn't cover, so a new enum still renders (untranslated)
                        rather than blanking. */}
                    <td>{t(`history.events.${item.type}`, { defaultValue: item.type })}</td>
                    <td>{item.rating ?? "—"}</td>
                    <td>{item.occurredAt ? item.occurredAt.slice(0, 10) : "—"}</td>
                  </tr>
                ))}
              </tbody>
            </table>
            {/* Honest footer: the table is paged, so say how much of the history is on screen —
                a 5 900-event profile must not silently read as 50 rows. */}
            <div className={styles.histFooter}>
              <span className="muted" data-testid="history-count">
                {t("history.showing", { shown: historyItems.length, total: historyTotal })}
              </span>
              {history.hasNextPage && (
                <button
                  type="button"
                  className="btn"
                  data-testid="history-more"
                  disabled={history.isFetchingNextPage}
                  onClick={() => history.fetchNextPage()}
                >
                  {history.isFetchingNextPage ? t("history.loadingMore") : t("history.showMore")}
                </button>
              )}
            </div>
          </>
        )}
      </section>

      <AccountCard />

      <Attributions />

      <SourcePicker
        profileId={profileId}
        open={pickerOpen}
        onOpenChange={setPickerOpen}
        onConnected={() => {
          qc.invalidateQueries({ queryKey: keys.history(profileId) });
          qc.invalidateQueries({ queryKey: keys.recommendations(profileId) });
        }}
      />
    </div>
  );
}
