import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useProfileId } from "../app/ProfileContext";
import { AvailabilityProvider } from "../components/Availability";
import { PosterCard } from "../components/PosterCard";
import { Loading } from "../components/states";
import { useAvailability, useRequestTitle, useSearch } from "../lib/queries";
import styles from "./routes.module.css";

export function Search(): React.JSX.Element {
  const { t } = useTranslation("search");
  const profileId = useProfileId();
  const [query, setQuery] = useState("");
  const [debounced, setDebounced] = useState("");

  useEffect(() => {
    const timer = setTimeout(() => setDebounced(query), 350);
    return () => clearTimeout(timer);
  }, [query]);

  const search = useSearch(profileId, debounced);
  const results = search.data?.results ?? [];
  const titleIds = useMemo(() => results.map((r) => r.titleId), [results]);
  const availability = useAvailability(profileId, titleIds);
  const request = useRequestTitle(profileId);

  const availabilityCtx = {
    configured: availability.data?.configured ?? false,
    results: availability.data?.results ?? {},
    requestingId: request.isPending ? (request.variables ?? null) : null,
    onRequest: (id: string) => request.mutate(id),
  };

  // 3-char minimum: at 2 chars a substring match returns junk (typing "tenet" pauses at "te" and
  // surfaces a dozen obscure "…té…" telenovelas). Mirrors the `enabled` gate in useSearch.
  const ready = debounced.trim().length >= 3;

  return (
    <div className={styles.page} data-testid="search">
      <h1 className={styles.pageTitle}>{t("heading")}</h1>
      <input
        type="search"
        className="field"
        data-testid="search-input"
        aria-label={t("inputLabel")}
        placeholder={t("placeholder")}
        value={query}
        // biome-ignore lint/a11y/noAutofocus: search is the whole point of this screen.
        autoFocus
        onChange={(e) => setQuery(e.target.value)}
      />

      {!ready ? (
        <p className="muted" style={{ marginTop: "var(--sp-4)" }}>
          {t("prompt")}
        </p>
      ) : search.isPending ? (
        <Loading label={t("searching")} />
      ) : results.length === 0 ? (
        <p className="muted" data-testid="search-empty" style={{ marginTop: "var(--sp-4)" }}>
          {t("noMatches", { query: debounced.trim() })}
        </p>
      ) : (
        <AvailabilityProvider value={availabilityCtx}>
          <div className={styles.searchGrid} data-testid="search-results">
            {results.map((item) => (
              // Show the taste-fit gauge when the backend sends a real confidence (profile has a
              // taste + embeddings); `hideNullFit` keeps it absent otherwise (older backend / no
              // taste) rather than showing a misleading neutral sliver on a raw catalog search.
              <PosterCard key={item.titleId} item={item} hideNullFit={true} />
            ))}
          </div>
        </AvailabilityProvider>
      )}
    </div>
  );
}
