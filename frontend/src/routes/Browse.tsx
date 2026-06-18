import { useMemo } from "react";
import { Link } from "react-router-dom";
import type { RecommendationItem } from "../api";
import { useProfileId } from "../app/ProfileContext";
import { AvailabilityProvider } from "../components/Availability";
import { ConfidenceMeter } from "../components/ConfidenceMeter";
import { RecRow } from "../components/RecRow";
import { ErrorState, Loading } from "../components/states";
import { posterTint } from "../lib/poster";
import {
  useAvailability,
  useDynamicRows,
  useRecommendations,
  useRequestTitle,
} from "../lib/queries";
import styles from "./routes.module.css";

function Hero({ item }: { item: RecommendationItem }): React.JSX.Element {
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
        <span className={styles.heroKicker}>Top pick tonight</span>
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

export function Browse(): React.JSX.Element {
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
    return <Loading label="Loading recommendations…" />;
  }
  if (recs.isError) {
    return <ErrorState error={recs.error} onRetry={() => recs.refetch()} />;
  }

  const rows = recs.data.rows.filter((r) => r.items.length > 0);
  const hero = rows.find((r) => r.key === "you_might_like")?.items[0] ?? rows[0]?.items[0];

  const availabilityCtx = {
    configured: availability.data?.configured ?? false,
    results: availability.data?.results ?? {},
    requestingId: request.isPending ? (request.variables ?? null) : null,
    onRequest: (id: string) => request.mutate(id),
  };

  return (
    <AvailabilityProvider value={availabilityCtx}>
      <div className={styles.page} data-testid="browse">
        {hero && <Hero item={hero} />}

        <Link to="/chat" className={styles.askChip} data-testid="ask-phare">
          Ask Phare for a mood…
        </Link>

        {rows.length === 0 ? (
          <p className="muted" data-testid="recs-empty">
            No recommendations yet — load sample data or connect a source.
          </p>
        ) : (
          rows.map((row) => <RecRow key={row.key} row={row} />)
        )}

        {dynamic.data?.rows.some((r) => r.items.length > 0) && (
          <>
            <h3 className={styles.pageTitle} style={{ marginTop: "var(--sp-5)" }}>
              Today's picks
            </h3>
            {dynamic.data.rows.map((row) => (
              <RecRow key={row.key} row={row} />
            ))}
          </>
        )}
      </div>
    </AvailabilityProvider>
  );
}
