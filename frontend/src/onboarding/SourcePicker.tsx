import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";
import { api } from "../api";
import { Sheet } from "../components/Sheet";
import { keys } from "../lib/queries";
import styles from "./onboarding.module.css";

type Active = "trakt" | "plex" | "jellyfin" | "seerr" | null;

function message(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/** The grouped connect sheet: watch-history sources, plus Seerr for requests & availability. */
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
  const qc = useQueryClient();
  const [active, setActive] = useState<Active>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [trakt, setTrakt] = useState<{ userCode: string; verificationUrl: string } | null>(null);

  // Plex / Jellyfin / Seerr form fields.
  const [baseUrl, setBaseUrl] = useState("");
  const [token, setToken] = useState("");
  const [userId, setUserId] = useState("");

  // Trakt device-flow polling can run for minutes; abort it when the sheet closes or unmounts so
  // it stops polling in the background and never stacks a second loop on reopen.
  const pollAbort = useRef<AbortController | null>(null);
  useEffect(() => {
    if (!open) {
      pollAbort.current?.abort();
    }
    return () => pollAbort.current?.abort();
  }, [open]);

  function finish() {
    // Connecting any source can change the source list and library availability.
    qc.invalidateQueries({ queryKey: keys.sources(profileId) });
    qc.invalidateQueries({ queryKey: keys.availability(profileId) });
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
    const controller = new AbortController();
    pollAbort.current?.abort(); // supersede any prior in-flight loop
    pollAbort.current = controller;
    const { signal } = controller;

    setBusy(true);
    setError(null);
    try {
      const start = await api.traktConnectStart();
      if (signal.aborted) return;
      setTrakt({ userCode: start.userCode, verificationUrl: start.verificationUrl });
      const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));
      const deadline = Date.now() + start.expiresIn * 1000;
      while (Date.now() < deadline) {
        await sleep(start.interval * 1000);
        if (signal.aborted) return;
        const poll = await api.traktConnectPoll(profileId, start.deviceCode);
        if (signal.aborted) return;
        if (poll.status === "connected") {
          await api.syncTrakt(profileId);
          if (signal.aborted) return;
          finish();
          return;
        }
        if (poll.status === "expired" || poll.status === "denied") {
          setError(`Trakt connection ${poll.status}.`);
          break;
        }
      }
    } catch (e) {
      if (!signal.aborted) setError(message(e));
    } finally {
      if (!signal.aborted) {
        setBusy(false);
        setTrakt(null);
      }
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

      <div className={styles.group}>Requests &amp; availability</div>

      <button
        type="button"
        className={styles.source}
        data-testid="source-seerr"
        onClick={() => select("seerr")}
        disabled={busy}
      >
        <div className={styles.sourceBody}>
          <div className={styles.sourceName}>Seerr</div>
          <div className={styles.sourceHint}>Request picks straight to your library</div>
        </div>
      </button>
      {active === "seerr" && (
        <div className={styles.form}>
          <input
            className="field"
            placeholder="Server URL (https://…)"
            value={baseUrl}
            onChange={(e) => setBaseUrl(e.target.value)}
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
            data-testid="connect-seerr"
            disabled={busy || baseUrl === "" || token === ""}
            onClick={() => submit(() => api.connectSeerr(profileId, baseUrl, token))}
          >
            Connect Seerr
          </button>
        </div>
      )}

      {error && <p className={styles.errorText}>{error}</p>}
    </Sheet>
  );
}
