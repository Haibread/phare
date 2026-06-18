import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import type { HistoryItem } from "../api";
import { useProfileId } from "../app/ProfileContext";
import { EditableChips } from "../components/EditableChips";
import { ErrorState, Loading } from "../components/states";
import { keys, useConversion, useHistory, useTaste, useUpdateTaste } from "../lib/queries";
import { SourcePicker } from "../onboarding/SourcePicker";
import styles from "./routes.module.css";

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
  const profileId = useProfileId();
  const qc = useQueryClient();
  const taste = useTaste(profileId);
  const history = useHistory(profileId);
  const conversion = useConversion(profileId);
  const updateTaste = useUpdateTaste(profileId);
  const [pickerOpen, setPickerOpen] = useState(false);

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
      <h1 className={styles.pageTitle}>Profile</h1>

      {/* Taste --------------------------------------------------------- */}
      <section className={styles.card} data-testid="taste-card">
        <div className={styles.cardHead}>
          <h2 style={{ fontSize: "1.05rem" }}>Your taste</h2>
          <span className="faint" style={{ fontSize: "0.75rem" }}>
            updates automatically
          </span>
        </div>

        {taste.isPending ? (
          <Loading label="Loading taste…" />
        ) : taste.data?.summary ? (
          <>
            <p className="muted" data-testid="taste-summary">
              {taste.data.summary}
            </p>
            <div style={{ marginTop: "var(--sp-3)" }}>
              <EditableChips
                label="Drawn to"
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
              label="Avoiding"
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
            Your taste profile builds automatically from your watch history, once an LLM is
            configured.
          </p>
        )}
      </section>

      {/* Sources ------------------------------------------------------- */}
      <section className={styles.card}>
        <div className={styles.cardHead}>
          <h2 style={{ fontSize: "1.05rem" }}>Connected sources</h2>
          <button
            type="button"
            className="btn"
            data-testid="add-source"
            onClick={() => setPickerOpen(true)}
          >
            Add
          </button>
        </div>
        <p className="muted" style={{ fontSize: "0.88rem" }}>
          Sync Trakt, Plex, or Jellyfin to keep your taste fresh.
        </p>
        {conv && (
          <p className="faint" data-testid="conversion" style={{ fontSize: "0.82rem" }}>
            {conv.rate === null
              ? `Conversion (top-${conv.topK}, ${conv.withinDays}d): not enough history yet.`
              : `Conversion: ${Math.round(conv.rate * 100)}% of ${conv.shown} shown were watched.`}
          </p>
        )}
      </section>

      {/* History ------------------------------------------------------- */}
      <section className={styles.card}>
        <h2 style={{ fontSize: "1.05rem", marginBottom: "var(--sp-3)" }}>History</h2>
        {history.isPending ? (
          <Loading label="Loading history…" />
        ) : history.isError ? (
          <ErrorState error={history.error} onRetry={() => history.refetch()} />
        ) : history.data.items.length === 0 ? (
          <p className="muted">No history yet.</p>
        ) : (
          <table className={styles.histTable} data-testid="history-table">
            <thead>
              <tr>
                <th>Title</th>
                <th>Event</th>
                <th>Rating</th>
                <th>When</th>
              </tr>
            </thead>
            <tbody>
              {history.data.items.slice(0, 50).map((item) => (
                <tr key={item.id} data-testid="history-row">
                  <td>
                    {item.title}
                    {episodeLabel(item)}
                  </td>
                  <td>{item.type}</td>
                  <td>{item.rating ?? "—"}</td>
                  <td>{item.occurredAt ? item.occurredAt.slice(0, 10) : "—"}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </section>

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
