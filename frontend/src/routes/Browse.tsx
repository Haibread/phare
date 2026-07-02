import { useMemo } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";
import type { RecommendationItem, RecommendationRow } from "../api";
import { useProfileId } from "../app/ProfileContext";
import { AvailabilityProvider } from "../components/Availability";
import { ConfidenceMeter } from "../components/ConfidenceMeter";
import { RecRow } from "../components/RecRow";
import { ErrorState, Loading } from "../components/states";
import { EmbeddingsDegradedContext } from "../lib/degraded";
import { posterTint } from "../lib/poster";
import {
  useAvailability,
  useDynamicRows,
  useRecommendations,
  useRequestTitle,
} from "../lib/queries";
import styles from "./routes.module.css";

function Hero({ item }: { item: RecommendationItem }): React.JSX.Element {
  const { t } = useTranslation("browse");
  return (
    <section className={styles.hero} data-testid="hero">
      <div
        className={styles.heroPoster}
        style={
          item.posterUrl
            ? {
                backgroundImage: `url(${item.posterUrl})`,
                backgroundSize: "cover",
                backgroundPosition: "center",
              }
            : { background: posterTint(item.titleId) }
        }
      />
      <div className={styles.heroBody}>
        <span className={styles.heroKicker}>{t("hero.topPick")}</span>
        <h2 className={styles.heroTitle}>
          {item.title}
          {item.year !== null && <span className="muted"> ({item.year})</span>}
        </h2>
        {item.explanation !== null && <p className="muted">{item.explanation}</p>}
        <ConfidenceMeter confidence={item.confidence} isSwing={item.isSwing} />
      </div>
    </section>
  );
}

/** The hero is the most premium slot, so it must never be a swing — a deliberate "stretch" there
 * would put "TOP PICK TONIGHT" and "a stretch" on the same card (review A6). First non-swing pick of
 * you_might_like (or the first row); if every candidate is a swing, there's no hero. */
export function pickHero(rows: RecommendationRow[]): RecommendationItem | undefined {
  const pool = rows.find((r) => r.key === "you_might_like")?.items ?? rows[0]?.items ?? [];
  return pool.find((item) => !item.isSwing);
}

export function Browse(): React.JSX.Element {
  const { t } = useTranslation("browse");
  const profileId = useProfileId();
  const recs = useRecommendations(profileId);
  const dynamic = useDynamicRows(profileId);

  // All visible title ids, fetched in one batch so cards can show a request/availability action.
  const titleIds = useMemo(() => {
    const ids = new Set<string>();
    for (const row of recs.data?.rows ?? []) {
      for (const item of row.items) ids.add(item.titleId);
    }
    for (const row of dynamic.data?.rows ?? []) {
      for (const item of row.items) ids.add(item.titleId);
    }
    return [...ids];
  }, [recs.data, dynamic.data]);
  const availability = useAvailability(profileId, titleIds);
  const request = useRequestTitle(profileId);

  if (recs.isPending) {
    return <Loading label={t("loading")} />;
  }
  if (recs.isError) {
    return <ErrorState error={recs.error} onRetry={() => recs.refetch()} />;
  }

  const allRows = recs.data.rows.filter((r) => r.items.length > 0);
  const hero = pickHero(allRows);
  // The hero is rendered as its own big card, so drop it from the rows below — otherwise it shows
  // twice (hero + first pick of you_might_like), which is where "The Wire ×4" started (review A7).
  const rows = hero
    ? allRows
        .map((r) => ({ ...r, items: r.items.filter((i) => i.titleId !== hero.titleId) }))
        .filter((r) => r.items.length > 0)
    : allRows;

  const availabilityCtx = {
    configured: availability.data?.configured ?? false,
    results: availability.data?.results ?? {},
    requestingId: request.isPending ? (request.variables ?? null) : null,
    onRequest: (id: string) => request.mutate(id),
  };

  return (
    <AvailabilityProvider value={availabilityCtx}>
      <EmbeddingsDegradedContext.Provider value={recs.data.embeddingsDegraded ?? false}>
        <div className={styles.page} data-testid="browse">
          {recs.data.embeddingsDegraded && (
            <p className={styles.offlineBanner} data-testid="embeddings-degraded-banner">
              {t("offlineBanner")}
            </p>
          )}
          {recs.data.profileBuilding && (
            <p className={styles.buildingBanner} data-testid="profile-building-banner">
              {t("buildingProfile")}
            </p>
          )}
          {hero && <Hero item={hero} />}

          <Link to="/chat" className={styles.askChip} data-testid="ask-phare">
            {t("askChip")}
          </Link>

          {rows.length === 0 ? (
            <p className="muted" data-testid="recs-empty">
              {t("empty")}
            </p>
          ) : (
            rows.map((row) => <RecRow key={row.key} row={row} />)
          )}

          {dynamic.data?.rows.some((r) => r.items.length > 0) && (
            <>
              <h3 className={styles.pageTitle} style={{ marginTop: "var(--sp-5)" }}>
                {t("todaysPicks")}
                {dynamic.data.degraded && (
                  <span className={styles.basicBadge} data-testid="dynamic-degraded">
                    {t("basicPicks")}
                  </span>
                )}
              </h3>
              {dynamic.data.rows.map((row) => (
                <RecRow key={row.key} row={row} />
              ))}
            </>
          )}
        </div>
      </EmbeddingsDegradedContext.Provider>
    </AvailabilityProvider>
  );
}
