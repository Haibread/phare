import { useCallback, useEffect, useState } from "react";
import { api, setAuthToken } from "./api";
import type {
  ChatReply,
  Conversion,
  HistoryItem,
  Profile,
  RecommendationItem,
  RecommendationRow,
  Taste,
} from "./api";
import { logger } from "./logger";

function stringList(value: unknown): string[] {
  return Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : [];
}

export function TastePanel({
  taste,
  busy,
  onGenerate,
}: {
  taste: Taste | null;
  busy: boolean;
  onGenerate: () => void;
}): React.JSX.Element {
  return (
    <div data-testid="taste-panel">
      <div className="row">
        <button type="button" data-testid="generate-taste" onClick={onGenerate} disabled={busy}>
          {taste ? "Regenerate taste profile" : "Generate taste profile"}
        </button>
      </div>
      {taste === null ? (
        <p className="muted">No taste profile yet. Generate one (requires an LLM key).</p>
      ) : (
        <div data-testid="taste-content">
          <p>{taste.summary}</p>
          <p className="muted">
            Likes: {stringList(taste.structured.likes).join(", ") || "—"} · Avoids:{" "}
            {stringList(taste.structured.hard_avoids).join(", ") || "—"}
            {taste.confidence !== null && ` · confidence ${taste.confidence}`}
          </p>
        </div>
      )}
    </div>
  );
}

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

export function RecCard({ item }: { item: RecommendationItem }): React.JSX.Element {
  return (
    <article className="rec-card" data-testid="rec-card">
      {item.isSwing && (
        <span className="badge" data-testid="swing-badge">
          Swing
        </span>
      )}
      <h4>
        {item.title}
        {item.year !== null && <span className="muted"> ({item.year})</span>}
      </h4>
      <p className="muted">{item.genres.join(", ") || "—"}</p>
      {item.explanation !== null && <p className="explanation">{item.explanation}</p>}
      {item.confidence !== null && (
        <p className="muted">confidence {Math.round(item.confidence * 100)}%</p>
      )}
    </article>
  );
}

export function RecRow({ row }: { row: RecommendationRow }): React.JSX.Element {
  return (
    <div data-testid="rec-row" data-row-key={row.key}>
      <h3>{row.title}</h3>
      <div className="rec-strip">
        {row.items.map((item) => (
          <RecCard key={item.titleId} item={item} />
        ))}
      </div>
    </div>
  );
}

export function ConversionBadge({ conversion }: { conversion: Conversion }): React.JSX.Element {
  if (conversion.rate === null) {
    return (
      <p className="muted" data-testid="conversion">
        Conversion (top-{conversion.topK}, {conversion.withinDays}d): not enough history yet.
      </p>
    );
  }
  return (
    <p className="muted" data-testid="conversion">
      Conversion (top-{conversion.topK}, {conversion.withinDays}d):{" "}
      {Math.round(conversion.rate * 100)}% of {conversion.shown} shown were watched.
    </p>
  );
}

export function Recommendations({
  rows,
  conversion,
  busy,
  onRefresh,
}: {
  rows: RecommendationRow[];
  conversion: Conversion | null;
  busy: boolean;
  onRefresh: () => void;
}): React.JSX.Element {
  return (
    <div data-testid="recommendations">
      <div className="row">
        <button type="button" data-testid="refresh-recs" onClick={onRefresh} disabled={busy}>
          Load recommendations
        </button>
      </div>
      {conversion !== null && <ConversionBadge conversion={conversion} />}
      {rows.length === 0 ? (
        <p className="muted" data-testid="recs-empty">
          No recommendations yet. Load sample data + catalog, then load recommendations.
        </p>
      ) : (
        rows.map((row) => <RecRow key={row.key} row={row} />)
      )}
    </div>
  );
}

type ChatTurn = { role: "user" | "agent"; text: string; items?: RecommendationItem[] };

export function ChatPanel({
  log,
  busy,
  onSend,
}: {
  log: ChatTurn[];
  busy: boolean;
  onSend: (message: string) => void;
}): React.JSX.Element {
  const [message, setMessage] = useState("");
  const submit = () => {
    const trimmed = message.trim();
    if (trimmed !== "") {
      onSend(trimmed);
      setMessage("");
    }
  };
  return (
    <div data-testid="chat-panel">
      <div className="chat-log">
        {log.map((turn, index) => (
          <div
            // Chat turns are append-only; index is a stable key here.
            key={`${turn.role}-${index}`}
            className={`chat-bubble ${turn.role}`}
            data-testid={`chat-${turn.role}`}
          >
            <p>{turn.text}</p>
            {turn.items && turn.items.length > 0 && (
              <div className="rec-strip">
                {turn.items.map((item) => (
                  <RecCard key={item.titleId} item={item} />
                ))}
              </div>
            )}
          </div>
        ))}
      </div>
      <div className="row">
        <input
          type="text"
          data-testid="chat-input"
          placeholder="e.g. something funny and short"
          value={message}
          onChange={(event) => setMessage(event.target.value)}
          onKeyDown={(event) => event.key === "Enter" && submit()}
        />
        <button
          type="button"
          data-testid="chat-send"
          onClick={submit}
          disabled={busy || message.trim() === ""}
        >
          Send
        </button>
      </div>
    </div>
  );
}

export function LoginGate({
  onLogin,
  error,
}: {
  onLogin: (password: string) => void;
  error: string | null;
}): React.JSX.Element {
  const [password, setPassword] = useState("");
  return (
    <main data-testid="login-gate">
      <h1>Phare</h1>
      <p className="muted">This instance is protected. Enter the password to continue.</p>
      <div className="row">
        <input
          type="password"
          data-testid="login-password"
          placeholder="Password"
          value={password}
          onChange={(event) => setPassword(event.target.value)}
          onKeyDown={(event) => event.key === "Enter" && onLogin(password)}
        />
        <button
          type="button"
          data-testid="login-submit"
          onClick={() => onLogin(password)}
          disabled={password === ""}
        >
          Log in
        </button>
      </div>
      {error !== null && (
        <p className="status" data-testid="login-error">
          {error}
        </p>
      )}
    </main>
  );
}

export function TraktConnectNotice({
  userCode,
  verificationUrl,
}: {
  userCode: string;
  verificationUrl: string;
}): React.JSX.Element {
  return (
    <p className="status" data-testid="trakt-connect-notice">
      Go to{" "}
      <a href={verificationUrl} target="_blank" rel="noreferrer">
        {verificationUrl}
      </a>{" "}
      and enter code <strong>{userCode}</strong>. Waiting for authorization…
    </p>
  );
}

export default function App(): React.JSX.Element {
  const [profiles, setProfiles] = useState<Profile[]>([]);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [history, setHistory] = useState<HistoryItem[]>([]);
  const [taste, setTaste] = useState<Taste | null>(null);
  const [rows, setRows] = useState<RecommendationRow[]>([]);
  const [conversion, setConversion] = useState<Conversion | null>(null);
  const [chatLog, setChatLog] = useState<ChatTurn[]>([]);
  const [newName, setNewName] = useState("");
  const [token, setToken] = useState("");
  const [traktConnect, setTraktConnect] = useState<{
    userCode: string;
    verificationUrl: string;
  } | null>(null);
  const [status, setStatus] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [needsLogin, setNeedsLogin] = useState(false);
  const [loginError, setLoginError] = useState<string | null>(null);

  const checkAuth = useCallback(async () => {
    const me = await api.me();
    setNeedsLogin(me.authRequired && !me.authenticated);
  }, []);

  useEffect(() => {
    checkAuth().catch((error: unknown) =>
      logger.warn("auth.check_failed", { error: String(error) }),
    );
  }, [checkAuth]);

  const onLogin = (password: string) => {
    setLoginError(null);
    api
      .login(password)
      .then(async (response) => {
        setAuthToken(response.token);
        setNeedsLogin(false);
        await checkAuth();
      })
      .catch((error: unknown) => {
        setLoginError(error instanceof Error ? error.message : String(error));
      });
  };

  const refreshProfiles = useCallback(async () => {
    const page = await api.listProfiles();
    setProfiles(page.items);
    setSelectedId((current) => current ?? page.items[0]?.id ?? null);
  }, []);

  const refreshHistory = useCallback(async (profileId: string) => {
    const page = await api.history(profileId);
    setHistory(page.items);
  }, []);

  const refreshTaste = useCallback(async (profileId: string) => {
    try {
      setTaste(await api.getTaste(profileId));
    } catch {
      setTaste(null); // 404 = not generated yet
    }
  }, []);

  useEffect(() => {
    if (needsLogin) {
      return;
    }
    refreshProfiles().catch((error: unknown) => {
      logger.error("profiles.load_failed", { error: String(error) });
      setStatus("Could not reach the backend. Is it running on :8000?");
    });
  }, [refreshProfiles, needsLogin]);

  useEffect(() => {
    if (selectedId === null) {
      setHistory([]);
      setTaste(null);
      setRows([]);
      setConversion(null);
      setChatLog([]);
      return;
    }
    setRows([]);
    setConversion(null);
    setChatLog([]);
    refreshHistory(selectedId).catch((error: unknown) =>
      setStatus(`Failed to load history: ${String(error)}`),
    );
    void refreshTaste(selectedId);
  }, [selectedId, refreshHistory, refreshTaste]);

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
      // Empty input → use the stored (OAuth-connected) token.
      const summary = await api.syncTrakt(selectedId, token.trim() || undefined);
      await refreshHistory(selectedId);
      return `Synced from Trakt: ${summary.created} new, ${summary.updated} updated.`;
    });

  const onConnectTrakt = async () => {
    if (selectedId === null) {
      setStatus("Select or create a profile first.");
      return;
    }
    setStatus(null);
    try {
      const start = await api.traktConnectStart();
      setTraktConnect({ userCode: start.userCode, verificationUrl: start.verificationUrl });
      const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));
      const deadline = Date.now() + start.expiresIn * 1000;
      while (Date.now() < deadline) {
        await sleep(start.interval * 1000);
        const poll = await api.traktConnectPoll(selectedId, start.deviceCode);
        if (poll.status === "connected") {
          setTraktConnect(null);
          setStatus("Trakt connected — you can sync now.");
          return;
        }
        if (poll.status === "expired" || poll.status === "denied") {
          setTraktConnect(null);
          setStatus(`Trakt connection ${poll.status}.`);
          return;
        }
      }
      setTraktConnect(null);
      setStatus("Trakt connection timed out — try again.");
    } catch (error: unknown) {
      setTraktConnect(null);
      setStatus(error instanceof Error ? error.message : String(error));
    }
  };

  const onGenerateTaste = () =>
    run(async () => {
      if (selectedId === null) {
        return "Select or create a profile first.";
      }
      setTaste(await api.generateTaste(selectedId));
      return "Generated taste profile.";
    });

  const onLoadCatalog = () =>
    run(async () => {
      const summary = await api.seedCatalog();
      return `Loaded sample catalog: ${summary.created} new titles.`;
    });

  const onRefreshRecs = () =>
    run(async () => {
      if (selectedId === null) {
        return "Select or create a profile first.";
      }
      const response = await api.recommendations(selectedId);
      setRows(response.rows);
      setConversion(await api.conversion(selectedId));
      const total = response.rows.reduce((sum, row) => sum + row.items.length, 0);
      return `Loaded ${total} recommendations across ${response.rows.length} rows.`;
    });

  const onSendChat = (message: string) =>
    run(async () => {
      if (selectedId === null) {
        return "Select or create a profile first.";
      }
      setChatLog((log) => [...log, { role: "user", text: message }]);
      const reply: ChatReply = await api.chat(selectedId, message);
      setChatLog((log) => [...log, { role: "agent", text: reply.replyText, items: reply.items }]);
      return `Chat: ${reply.items.length} suggestions.`;
    });

  if (needsLogin) {
    return <LoginGate onLogin={onLogin} error={loginError} />;
  }

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
          <button type="button" data-testid="load-catalog" onClick={onLoadCatalog} disabled={busy}>
            Load sample catalog
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
            data-testid="connect-trakt"
            onClick={onConnectTrakt}
            disabled={busy || selectedId === null || traktConnect !== null}
          >
            Connect Trakt
          </button>
          <button
            type="button"
            data-testid="sync-trakt"
            onClick={onSync}
            disabled={busy || selectedId === null}
          >
            Sync from Trakt
          </button>
        </div>
        {traktConnect !== null && (
          <TraktConnectNotice
            userCode={traktConnect.userCode}
            verificationUrl={traktConnect.verificationUrl}
          />
        )}
      </section>

      {status && (
        <p className="status" data-testid="status">
          {status}
        </p>
      )}

      <section>
        <h2>Taste profile</h2>
        <TastePanel taste={taste} busy={busy} onGenerate={onGenerateTaste} />
      </section>

      <section>
        <h2>Recommendations</h2>
        <Recommendations
          rows={rows}
          conversion={conversion}
          busy={busy}
          onRefresh={onRefreshRecs}
        />
      </section>

      <section>
        <h2>Chat</h2>
        <p className="muted">
          Tell the agent your mood — "tired, something funny under 90 minutes".
        </p>
        <ChatPanel log={chatLog} busy={busy} onSend={onSendChat} />
      </section>

      <section>
        <h2>History</h2>
        <HistoryTable items={history} />
      </section>
    </main>
  );
}
