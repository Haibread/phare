import { useState } from "react";
import { api } from "../api";
import { Sheet } from "../components/Sheet";
import styles from "./onboarding.module.css";

type Active = "trakt" | "plex" | "jellyfin" | null;

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/** The grouped connect sheet. Watch-history sources only for now (Seerr's "requests &
 * availability" group lands with the action-provider work). */
export function SourcePicker({
  profileId,
  open,
  onOpenChange,
  onConnected,
}: {
  profileId: string;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  onConnected: () => void;
}): React.JSX.Element {
  const [active, setActive] = useState<Active>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [trakt, setTrakt] = useState<{ userCode: string; verificationUrl: string } | null>(null);

  // Plex / Jellyfin form fields.
  const [baseUrl, setBaseUrl] = useState("");
  const [token, setToken] = useState("");
  const [userId, setUserId] = useState("");

  function finish() {
    onConnected();
    onOpenChange(false);
  }

  function select(next: Active) {
    setError(null);
    setActive(next);
    if (next === "trakt") {
      void connectTrakt();
    }
  }

  async function connectTrakt() {
    setBusy(true);
    setError(null);
    try {
      const start = await api.traktConnectStart();
      setTrakt({ userCode: start.userCode, verificationUrl: start.verificationUrl });
      const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
      const deadline = Date.now() + start.expiresIn * 1000;
      while (Date.now() < deadline) {
        await sleep(start.interval * 1000);
        const poll = await api.traktConnectPoll(profileId, start.deviceCode);
        if (poll.status === "connected") {
          await api.syncTrakt(profileId);
          finish();
          return;
        }
        if (poll.status === "expired" || poll.status === "denied") {
          setError(`Trakt connection ${poll.status}.`);
          break;
        }
      }
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy(false);
      setTrakt(null);
    }
  }

  async function submit(run: () => Promise<unknown>) {
    setBusy(true);
    setError(null);
    try {
      await run();
      finish();
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <Sheet
      open={open}
      onOpenChange={onOpenChange}
      title="Connect a source"
      description="Phare reads your own watch history — never anyone else's."
    >
      <div className={styles.group}>Watch history</div>

      <button
        type="button"
        className={styles.source}
        data-testid="source-trakt"
        onClick={() => select("trakt")}
        disabled={busy}
      >
        <div className={styles.sourceBody}>
          <div className={styles.sourceName}>Trakt</div>
          <div className={styles.sourceHint}>Ratings, history &amp; watchlist</div>
        </div>
      </button>
      {active === "trakt" && trakt && (
        <p className={styles.notice} data-testid="trakt-connect-notice">
          Go to{" "}
          <a href={trakt.verificationUrl} target="_blank" rel="noreferrer">
            {trakt.verificationUrl}
          </a>{" "}
          and enter <strong>{trakt.userCode}</strong>. Waiting for authorization…
        </p>
      )}

      <button
        type="button"
        className={styles.source}
        data-testid="source-plex"
        onClick={() => select("plex")}
        disabled={busy}
      >
        <div className={styles.sourceBody}>
          <div className={styles.sourceName}>Plex</div>
          <div className={styles.sourceHint}>Your server's watch history</div>
        </div>
      </button>
      {active === "plex" && (
        <div className={styles.form}>
          <input
            className="field"
            placeholder="Server URL (https://…)"
            value={baseUrl}
            onChange={(e) => setBaseUrl(e.target.value)}
          />
          <input
            className="field"
            placeholder="Plex token"
            value={token}
            onChange={(e) => setToken(e.target.value)}
          />
          <button
            type="button"
            className="btn btn-primary"
            disabled={busy || baseUrl === "" || token === ""}
            onClick={() => submit(() => api.syncPlex(profileId, baseUrl, token))}
          >
            Connect Plex
          </button>
        </div>
      )}

      <button
        type="button"
        className={styles.source}
        data-testid="source-jellyfin"
        onClick={() => select("jellyfin")}
        disabled={busy}
      >
        <div className={styles.sourceBody}>
          <div className={styles.sourceName}>Jellyfin</div>
          <div className={styles.sourceHint}>Your server's watch history</div>
        </div>
      </button>
      {active === "jellyfin" && (
        <div className={styles.form}>
          <input
            className="field"
            placeholder="Server URL (https://…)"
            value={baseUrl}
            onChange={(e) => setBaseUrl(e.target.value)}
          />
          <input
            className="field"
            placeholder="User ID"
            value={userId}
            onChange={(e) => setUserId(e.target.value)}
          />
          <input
            className="field"
            placeholder="API key"
            value={token}
            onChange={(e) => setToken(e.target.value)}
          />
          <button
            type="button"
            className="btn btn-primary"
            disabled={busy || baseUrl === "" || userId === "" || token === ""}
            onClick={() => submit(() => api.syncJellyfin(profileId, baseUrl, userId, token))}
          >
            Connect Jellyfin
          </button>
        </div>
      )}

      {error && <p className={styles.errorText}>{error}</p>}
    </Sheet>
  );
}
