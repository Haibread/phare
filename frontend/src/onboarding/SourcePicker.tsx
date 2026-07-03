import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useRef, useState } from "react";
import { Trans, useTranslation } from "react-i18next";
import { type JellyfinUser, type SourceCapabilities, api } from "../api";
import { Sheet } from "../components/Sheet";
import { keys, useSourceCapabilities } from "../lib/queries";
import styles from "./onboarding.module.css";

type Active = "trakt" | "plex" | "jellyfin" | "seerr" | null;

// Consecutive progress-poll failures before we tell the user the counter may be stalled (review D3).
const STALL_THRESHOLD = 3;

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
  /** Called once a source is connected, with how many history items were imported (0 for a
   * request-only source like Seerr) so the caller can confirm the count before landing (review D4). */
  onConnected: (importedCount: number) => void;
}): React.JSX.Element {
  const { t } = useTranslation("onboarding");
  const qc = useQueryClient();
  const capabilities = useSourceCapabilities();
  const [active, setActive] = useState<Active>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [trakt, setTrakt] = useState<{ userCode: string; verificationUrl: string } | null>(null);

  // A history import (Trakt/Plex/Jellyfin) can run for minutes. Rather than block on the await with
  // no feedback, we flip into a "syncing" view and poll `GET /history` for the running `total`,
  // which the backend now commits incrementally as it ingests.
  const [syncing, setSyncing] = useState(false);
  const [count, setCount] = useState(0);
  // When the progress poll fails repeatedly the counter freezes silently; surface that + a retry
  // instead (review D3). The import POST itself keeps running independently.
  const [stalled, setStalled] = useState(false);

  // Plex / Jellyfin / Seerr form fields.
  const [baseUrl, setBaseUrl] = useState("");
  const [token, setToken] = useState("");
  // Jellyfin: fetched user list + the picked id, replacing the raw GUID field (review D2).
  const [jfUsers, setJfUsers] = useState<JellyfinUser[] | null>(null);
  const [jfUserId, setJfUserId] = useState("");

  // Trakt device-flow polling can run for minutes; abort it when the sheet closes or unmounts so
  // it stops polling in the background and never stacks a second loop on reopen.
  const pollAbort = useRef<AbortController | null>(null);
  // Interval that polls the import progress count; cleared on completion/error/close/unmount.
  const countTimer = useRef<ReturnType<typeof setInterval> | null>(null);
  const pollFails = useRef(0);

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

  /** Is a source usable on this server? Defaults to `true` while capabilities load so buttons don't
   * flash disabled; once known, unconfigured sources are greyed out (review D1). */
  function capable(source: keyof SourceCapabilities): boolean {
    return capabilities.data ? capabilities.data[source] : true;
  }

  function finish(importedCount = 0) {
    // Connecting any source can change the source list and library availability.
    qc.invalidateQueries({ queryKey: keys.sources(profileId) });
    qc.invalidateQueries({ queryKey: keys.availability(profileId) });
    onConnected(importedCount);
    onOpenChange(false);
  }

  /** Poll `GET /history` `total` once; track consecutive failures to detect a stalled counter. */
  const pollCount = useCallback(() => {
    api
      .history(profileId)
      .then((page) => {
        pollFails.current = 0;
        setStalled(false);
        setCount(page.total);
      })
      .catch(() => {
        // A transient poll failure shouldn't abort the import; keep the last known count, but after
        // a few in a row tell the user the progress display may be stuck (review D3).
        pollFails.current += 1;
        if (pollFails.current >= STALL_THRESHOLD) {
          setStalled(true);
        }
      });
  }, [profileId]);

  /** Flip into the syncing view, poll `GET /history` `total` every ~2s for live progress, and run
   * the actual sync. On resolve: invalidate + close. On reject: surface the error, leave syncing. */
  async function runSync(sync: () => Promise<unknown>): Promise<void> {
    setBusy(true);
    setError(null);
    setTrakt(null); // hide the Trakt device-code notice once the import starts
    setCount(0);
    setStalled(false);
    pollFails.current = 0;
    setSyncing(true);

    stopCountPolling();
    countTimer.current = setInterval(pollCount, 2000);

    try {
      await sync();
      stopCountPolling();
      // One final read for the accurate imported total (the 2s poll may lag the last batch).
      const total = await api
        .history(profileId)
        .then((page) => page.total)
        .catch(() => count);
      setSyncing(false);
      setBusy(false);
      finish(total);
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

  async function loadJellyfinUsers() {
    setBusy(true);
    setError(null);
    try {
      const users = await api.jellyfinUsers(baseUrl, token);
      setJfUsers(users);
      setJfUserId(users[0]?.id ?? "");
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy(false);
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
          {stalled && (
            <p className={styles.stallNote} data-testid="sync-stalled">
              {t("sources.stalled")}{" "}
              <button
                type="button"
                className={styles.stallRetry}
                onClick={() => {
                  pollFails.current = 0;
                  setStalled(false);
                  pollCount();
                }}
              >
                {t("sources.retryProgress")}
              </button>
            </p>
          )}
          {error && <p className={styles.errorText}>{error}</p>}
        </output>
      </Sheet>
    );
  }

  /** A source row + an optional "not configured on this server" note when the operator hasn't set
   * up that source's server-side prerequisites (review D1). */
  function sourceButton(
    source: Exclude<Active, null>,
    testid: string,
    name: string,
    hint: string,
  ): React.JSX.Element {
    const enabled = capable(source);
    return (
      <>
        <button
          type="button"
          className={styles.source}
          data-testid={testid}
          onClick={() => select(source)}
          disabled={busy || !enabled}
        >
          <div className={styles.sourceBody}>
            <div className={styles.sourceName}>{name}</div>
            <div className={styles.sourceHint}>{hint}</div>
          </div>
        </button>
        {!enabled && (
          <p className={styles.softHint} data-testid={`${testid}-unconfigured`}>
            {t("sources.notConfigured")}
          </p>
        )}
      </>
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

      {sourceButton("trakt", "source-trakt", "Trakt", t("sources.trakt.hint"))}
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

      {sourceButton("plex", "source-plex", "Plex", t("sources.plex.hint"))}
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

      {sourceButton("jellyfin", "source-jellyfin", "Jellyfin", t("sources.jellyfin.hint"))}
      {active === "jellyfin" && (
        <div className={styles.form}>
          <input
            className="field"
            placeholder={t("sources.form.serverUrl")}
            value={baseUrl}
            onChange={(e) => {
              setBaseUrl(e.target.value);
              setJfUsers(null); // credentials changed — the fetched user list is stale
            }}
          />
          <input
            className="field"
            placeholder={t("sources.form.apiKey")}
            value={token}
            onChange={(e) => {
              setToken(e.target.value);
              setJfUsers(null);
            }}
          />
          {jfUsers === null ? (
            <button
              type="button"
              className="btn"
              data-testid="jellyfin-list-users"
              disabled={busy || baseUrl === "" || token === ""}
              onClick={() => void loadJellyfinUsers()}
            >
              {t("sources.jellyfin.listUsers")}
            </button>
          ) : (
            <>
              <select
                className="field"
                data-testid="jellyfin-user-select"
                aria-label={t("sources.jellyfin.userLabel")}
                value={jfUserId}
                onChange={(e) => setJfUserId(e.target.value)}
              >
                {jfUsers.map((u) => (
                  <option key={u.id} value={u.id}>
                    {u.name}
                  </option>
                ))}
              </select>
              <button
                type="button"
                className="btn btn-primary"
                disabled={busy || jfUserId === ""}
                onClick={() =>
                  void runSync(() => api.syncJellyfin(profileId, baseUrl, jfUserId, token))
                }
              >
                {t("sources.jellyfin.connect")}
              </button>
            </>
          )}
        </div>
      )}

      <div className={styles.group}>{t("sources.groups.requests")}</div>

      {sourceButton("seerr", "source-seerr", "Seerr", t("sources.seerr.hint"))}
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
