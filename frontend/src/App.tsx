import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import type { HistoryItem, Profile } from "./api";
import { logger } from "./logger";

function episodeLabel(item: HistoryItem): string {
  if (item.seasonNumber !== null && item.episodeNumber !== null) {
    return ` S${item.seasonNumber}E${item.episodeNumber}`;
  }
  return "";
}

export function HistoryTable({ items }: { items: HistoryItem[] }): React.JSX.Element {
  if (items.length === 0) {
    return (
      <p className="muted" data-testid="history-empty">
        No history yet. Load sample data or sync from Trakt.
      </p>
    );
  }
  return (
    <table data-testid="history-table">
      <thead>
        <tr>
          <th>Title</th>
          <th>Kind</th>
          <th>Event</th>
          <th>Rating</th>
          <th>When</th>
        </tr>
      </thead>
      <tbody>
        {items.map((item) => (
          <tr key={item.id} data-testid="history-row">
            <td>
              {item.title}
              {episodeLabel(item)}
            </td>
            <td>{item.kind}</td>
            <td>{item.type}</td>
            <td>{item.rating ?? "—"}</td>
            <td>{item.occurredAt ? item.occurredAt.slice(0, 10) : "—"}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

export default function App(): React.JSX.Element {
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [history, setHistory] = useState<HistoryItem[]>([]);
  const [newName, setNewName] = useState("");
  const [token, setToken] = useState("");
  const [status, setStatus] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const refreshProfiles = useCallback(async () => {
    const page = await api.listProfiles();
    setProfiles(page.items);
    setSelectedId((current) => current ?? page.items[0]?.id ?? null);
  }, []);

  const refreshHistory = useCallback(async (profileId: string) => {
    const page = await api.history(profileId);
    setHistory(page.items);
  }, []);

  useEffect(() => {
    refreshProfiles().catch((error: unknown) => {
      logger.error("profiles.load_failed", { error: String(error) });
      setStatus("Could not reach the backend. Is it running on :8000?");
    });
  }, [refreshProfiles]);

  useEffect(() => {
    if (selectedId === null) {
      setHistory([]);
      return;
    }
    refreshHistory(selectedId).catch((error: unknown) =>
      setStatus(`Failed to load history: ${String(error)}`),
    );
  }, [selectedId, refreshHistory]);

  async function run(action: () => Promise<string>): Promise<void> {
    setBusy(true);
    setStatus(null);
    try {
      setStatus(await action());
    } catch (error: unknown) {
      setStatus(error instanceof Error ? error.message : String(error));
    } finally {
      setBusy(false);
    }
  }

  const onCreateProfile = () =>
    run(async () => {
      const profile = await api.createProfile(newName.trim());
      setNewName("");
      await refreshProfiles();
      setSelectedId(profile.id);
      return `Created profile "${profile.displayName}".`;
    });

  const onLoadSample = () =>
    run(async () => {
      if (selectedId === null) {
        return "Select or create a profile first.";
      }
      const summary = await api.loadSampleData(selectedId);
      await refreshHistory(selectedId);
      return `Loaded sample data: ${summary.created} new events.`;
    });

  const onSync = () =>
    run(async () => {
      if (selectedId === null) {
        return "Select or create a profile first.";
      }
      const summary = await api.syncTrakt(selectedId, token.trim());
      await refreshHistory(selectedId);
      return `Synced from Trakt: ${summary.created} new, ${summary.updated} updated.`;
    });

  return (
    <main>
      <h1>Phare</h1>
      <p className="muted">Self-hosted movie &amp; TV recommendations.</p>

      <section>
        <h2>Profile</h2>
        <div className="row">
          <select
            data-testid="profile-select"
            value={selectedId ?? ""}
            onChange={(event) => setSelectedId(event.target.value || null)}
            disabled={profiles.length === 0}
          >
            {profiles.length === 0 && <option value="">No profiles yet</option>}
            {profiles.map((profile) => (
              <option key={profile.id} value={profile.id}>
                {profile.displayName}
              </option>
            ))}
          </select>
        </div>
        <div className="row">
          <input
            type="text"
            data-testid="new-profile-name"
            placeholder="New profile name"
            value={newName}
            onChange={(event) => setNewName(event.target.value)}
          />
          <button
            type="button"
            data-testid="create-profile"
            onClick={onCreateProfile}
            disabled={busy || newName.trim() === ""}
          >
            Create
          </button>
        </div>
      </section>

      <section>
        <h2>Get data in</h2>
        <div className="row">
          <button
            type="button"
            data-testid="load-sample"
            onClick={onLoadSample}
            disabled={busy || selectedId === null}
          >
            Load sample data
          </button>
        </div>
        <div className="row">
          <input
            type="password"
            data-testid="trakt-token"
            placeholder="Trakt access token"
            value={token}
            onChange={(event) => setToken(event.target.value)}
          />
          <button
            type="button"
            data-testid="sync-trakt"
            onClick={onSync}
            disabled={busy || selectedId === null || token.trim() === ""}
          >
            Sync from Trakt
          </button>
        </div>
      </section>

      {status && (
        <p className="status" data-testid="status">
          {status}
        </p>
      )}

      <section>
        <h2>History</h2>
        <HistoryTable items={history} />
      </section>
    </main>
  );
}
