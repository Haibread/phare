import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import type { RecommendationItem } from "../api";
import { BeaconGlyph } from "../components/Brand";
import { ErrorState, Loading } from "../components/states";
import { useGenerateTaste, useSearch, useSeedLovedTitle } from "../lib/queries";
import styles from "./onboarding.module.css";

/** How many loved picks we nudge toward. One is enough to unblock Browse (an honest, thin profile),
 * but three gives the taste extractor something to work with — so we allow >=1 and encourage >=3. */
const ENCOURAGED_PICKS = 3;

/** "Start from scratch": for a user with no Trakt/Plex/Jellyfin, seed a taste profile by hand. They
 * search the catalog, pick a handful of titles they loved (each logs a `loved` watched+liked pair),
 * then "C'est parti" generates the taste and lands them in Browse. The seeded profile is thin and
 * honest — Browse's `profile_building` state already handles that gracefully (round 7, fix 1). */
export function ScratchStart({
  profileId,
  onDone,
}: {
  profileId: string;
  /** Called once picks are logged + taste generation kicked off — the caller reveals Browse. */
  onDone: () => void;
}): React.JSX.Element {
  const { t } = useTranslation("onboarding");
  const [query, setQuery] = useState("");
  const [debounced, setDebounced] = useState("");
  // Picked titles, keyed by id, in pick order — rendered as removable chips.
  const [picks, setPicks] = useState<RecommendationItem[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const [submitError, setSubmitError] = useState(false);

  const seedLoved = useSeedLovedTitle(profileId);
  const generateTaste = useGenerateTaste(profileId);

  useEffect(() => {
    const timer = setTimeout(() => setDebounced(query), 350);
    return () => clearTimeout(timer);
  }, [query]);

  const search = useSearch(profileId, debounced);
  const ready = debounced.trim().length >= 3;
  const pickedIds = useMemo(() => new Set(picks.map((p) => p.titleId)), [picks]);
  // Hide already-picked titles from the results so a pick can't be added twice.
  const results = (search.data?.results ?? []).filter((r) => !pickedIds.has(r.titleId));

  function addPick(item: RecommendationItem): void {
    setPicks((prev) => (prev.some((p) => p.titleId === item.titleId) ? prev : [...prev, item]));
    setQuery("");
    setDebounced("");
  }

  function removePick(titleId: string): void {
    setPicks((prev) => prev.filter((p) => p.titleId !== titleId));
  }

  async function finish(): Promise<void> {
    if (picks.length === 0 || submitting) {
      return;
    }
    setSubmitting(true);
    setSubmitError(false);
    try {
      // Log each loved pick, then generate the (thin) taste. Sequential so a mid-list failure is
      // surfaced instead of silently partially seeding.
      for (const pick of picks) {
        await seedLoved.mutateAsync(pick.titleId);
      }
      // Best-effort taste generation: offline (no LLM) this 503s, but the seeded events already
      // unblock Browse, so we proceed regardless — Browse personalises from the centroid.
      await generateTaste.mutateAsync().catch(() => undefined);
      onDone();
    } catch {
      setSubmitError(true);
      setSubmitting(false);
    }
  }

  const enough = picks.length >= ENCOURAGED_PICKS;

  return (
    <main className={`${styles.cold} ${styles.scratch}`} data-testid="scratch-start">
      <span className={styles.halo} aria-hidden="true">
        <BeaconGlyph />
      </span>
      <h1 className={styles.coldTitle}>{t("scratch.title")}</h1>
      <p className={styles.lede}>{t("scratch.lede")}</p>

      <input
        type="search"
        className="field"
        data-testid="scratch-search"
        aria-label={t("scratch.searchLabel")}
        placeholder={t("scratch.searchPlaceholder")}
        value={query}
        onChange={(e) => setQuery(e.target.value)}
      />

      {picks.length > 0 && (
        <ul className={styles.pickChips} data-testid="scratch-picks">
          {picks.map((pick) => (
            <li key={pick.titleId} className={styles.pickChip} data-testid="scratch-pick">
              <span>
                {pick.title}
                {pick.year !== null && <span className="faint"> ({pick.year})</span>}
              </span>
              <button
                type="button"
                className={styles.pickRemove}
                aria-label={t("scratch.remove", { title: pick.title })}
                data-testid="scratch-remove"
                onClick={() => removePick(pick.titleId)}
              >
                ×
              </button>
            </li>
          ))}
        </ul>
      )}

      {ready &&
        (search.isPending ? (
          <Loading label={t("scratch.searching")} />
        ) : results.length === 0 ? (
          <p className="muted" data-testid="scratch-no-matches">
            {t("scratch.noMatches", { query: debounced.trim() })}
          </p>
        ) : (
          <ul className={styles.resultList} data-testid="scratch-results">
            {results.map((item) => (
              <li key={item.titleId}>
                <button
                  type="button"
                  className={styles.resultRow}
                  data-testid="scratch-result"
                  onClick={() => addPick(item)}
                >
                  <span className={styles.resultName}>{item.title}</span>
                  {item.year !== null && <span className="faint">{item.year}</span>}
                </button>
              </li>
            ))}
          </ul>
        ))}

      <p className="faint" data-testid="scratch-hint" style={{ fontSize: "0.8rem" }}>
        {enough ? t("scratch.hintEnough") : t("scratch.hint", { count: ENCOURAGED_PICKS })}
      </p>

      <button
        type="button"
        className={`btn btn-primary ${styles.cta}`}
        data-testid="scratch-go"
        disabled={picks.length === 0 || submitting}
        onClick={() => void finish()}
      >
        {submitting ? t("scratch.seeding") : t("scratch.go")}
      </button>

      {submitError && <ErrorState error={seedLoved.error ?? new Error("seed failed")} />}
    </main>
  );
}
