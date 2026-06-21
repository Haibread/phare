import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState } from "react";
import { Trans, useTranslation } from "react-i18next";
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
  const { t } = useTranslation("onboarding");
  const qc = useQueryClient();
  const [active, setActive] = useState<Active>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [trakt, setTrakt] = useState<{ userCode: string; verificationUrl: string } | null>(null);

  // A history import (Trakt/Plex/Jellyfin) can run for minutes. Rather than block on the await with
  // no feedback, we flip into a "syncing" view and poll `GET /history` for the running `total`,
  // which the backend now commits incrementally as it ingests.
  const [syncing, setSyncing] = useState(false);
  const [count, setCount] = useState(0);

  // Plex / Jellyfin / Seerr form fields.
  const [baseUrl, setBaseUrl] = useState("");
  const [token, setToken] = useState("");
  const [userId, setUserId] = useState("");

  // Trakt device-flow polling can run for minutes; abort it when the sheet closes or unmounts so
  // it stops polling in the background and never stacks a second loop on reopen.
  const pollAbort = useRef<AbortController | null>(null);
  // Interval that polls the import progress count; cleared on completion/error/close/unmount.
  const countTimer = useRef<ReturnType<typeof setInterval> | null>(null);

  const stopCountPolling = useCallback(() => {
    if (countTimer.current !== null) {
      clearInterval(countTimer.current);
      countTimer.current = null;
    }
  }, []);

  useEffect(() => {
    if (!open) {
      pollAbort.current?.abort();
      stopCountPolling();
    }
    return () => {
      pollAbort.current?.abort();
      stopCountPolling();
    };
  }, [open, stopCountPolling]);

  function finish() {
    // Connecting any source can change the source list and library availability.
    qc.invalidateQueries({ queryKey: keys.sources(profileId) });
    qc.invalidateQueries({ queryKey: keys.availability(profileId) });
    onConnected();
    onOpenChange(false);
  }

  /** Flip into the syncing view, poll `GET /history` `total` every ~2s for live progress, and run
   * the actual sync. On resolve: invalidate + close. On reject: surface the error, leave syncing. */
  async function runSync(sync: () => Promise<unknown>): Promise<void> {
    setBusy(true);
    setError(null);
    setTrakt(null); // hide the Trakt device-code notice once the import starts
    setCount(0);
    setSyncing(true);

    stopCountPolling();
    countTimer.current = setInterval(() => {
      api
        .history(profileId)
        .then((page) => setCount(page.total))
        .catch(() => {
          // A transient poll failure shouldn't abort the import; keep the last known count.
        });
    }, 2000);

    try {
      await sync();
      stopCountPolling();
      setSyncing(false);
      setBusy(false);
      finish();
    } catch (e) {
      stopCountPolling();
      setSyncing(false);
      setBusy(false);
      setError(message(e));
    }
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
          // Device flow is done; hand off to the shared syncing view + progress poll.
          await runSync(() => api.syncTrakt(profileId));
          return;
        }
        if (poll.status === "expired" || poll.status === "denied") {
          setError(t("sources.trakt.connectionFailed", { status: poll.status }));
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

  /** Seerr connects instantly (no history import), so it keeps the plain blocking flow. */
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

  if (syncing) {
    return (
      <Sheet
        open={open}
        onOpenChange={onOpenChange}
        title={t("sources.title")}
        description={t("sources.description")}
      >
        <output className={styles.syncing} data-testid="sync-progress">
          <span className={styles.syncSpinner} aria-hidden="true" />
          <p className={styles.syncMessage}>
            <Trans
              t={t}
              i18nKey="sources.importing"
              count={count}
              values={{ count }}
              components={{ c: <strong data-testid="sync-progress-count" /> }}
            />
          </p>
          {error && <p className={styles.errorText}>{error}</p>}
        </output>
      </Sheet>
    );
  }

  return (
    <Sheet
      open={open}
      onOpenChange={onOpenChange}
      title={t("sources.title")}
      description={t("sources.description")}
    >
      <div className={styles.group}>{t("sources.groups.watchHistory")}</div>

      <button
        type="button"
        className={styles.source}
        data-testid="source-trakt"
        onClick={() => select("trakt")}
        disabled={busy}
      >
        <div className={styles.sourceBody}>
          <div className={styles.sourceName}>Trakt</div>
          <div className={styles.sourceHint}>{t("sources.trakt.hint")}</div>
        </div>
      </button>
      {active === "trakt" && trakt && (
        <p className={styles.notice} data-testid="trakt-connect-notice">
          <Trans
            t={t}
            i18nKey="sources.trakt.notice"
            values={{ url: trakt.verificationUrl, code: trakt.userCode }}
            components={{
              lnk: (
                <a href={trakt.verificationUrl} target="_blank" rel="noreferrer">
                  {trakt.verificationUrl}
                </a>
              ),
              b: <strong />,
            }}
          />
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
          <div className={styles.sourceHint}>{t("sources.plex.hint")}</div>
        </div>
      </button>
      {active === "plex" && (
        <div className={styles.form}>
          <input
            className="field"
            placeholder={t("sources.form.serverUrl")}
            value={baseUrl}
            onChange={(e) => setBaseUrl(e.target.value)}
          />
          <input
            className="field"
            placeholder={t("sources.plex.token")}
            value={token}
            onChange={(e) => setToken(e.target.value)}
          />
          <button
            type="button"
            className="btn btn-primary"
            disabled={busy || baseUrl === "" || token === ""}
            onClick={() => void runSync(() => api.syncPlex(profileId, baseUrl, token))}
          >
            {t("sources.plex.connect")}
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
          <div className={styles.sourceHint}>{t("sources.jellyfin.hint")}</div>
        </div>
      </button>
      {active === "jellyfin" && (
        <div className={styles.form}>
          <input
            className="field"
            placeholder={t("sources.form.serverUrl")}
            value={baseUrl}
            onChange={(e) => setBaseUrl(e.target.value)}
          />
          <input
            className="field"
            placeholder={t("sources.form.userId")}
            value={userId}
            onChange={(e) => setUserId(e.target.value)}
          />
          <input
            className="field"
            placeholder={t("sources.form.apiKey")}
            value={token}
            onChange={(e) => setToken(e.target.value)}
          />
          <button
            type="button"
            className="btn btn-primary"
            disabled={busy || baseUrl === "" || userId === "" || token === ""}
            onClick={() => void runSync(() => api.syncJellyfin(profileId, baseUrl, userId, token))}
          >
            {t("sources.jellyfin.connect")}
          </button>
        </div>
      )}

      <div className={styles.group}>{t("sources.groups.requests")}</div>

      <button
        type="button"
        className={styles.source}
        data-testid="source-seerr"
        onClick={() => select("seerr")}
        disabled={busy}
      >
        <div className={styles.sourceBody}>
          <div className={styles.sourceName}>Seerr</div>
          <div className={styles.sourceHint}>{t("sources.seerr.hint")}</div>
        </div>
      </button>
      {active === "seerr" && (
        <div className={styles.form}>
          <input
            className="field"
            placeholder={t("sources.form.serverUrl")}
            value={baseUrl}
            onChange={(e) => setBaseUrl(e.target.value)}
          />
          <input
            className="field"
            placeholder={t("sources.form.apiKey")}
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
            {t("sources.seerr.connect")}
          </button>
        </div>
      )}

      {error && <p className={styles.errorText}>{error}</p>}
    </Sheet>
  );
}
