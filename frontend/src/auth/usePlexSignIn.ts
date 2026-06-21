/** Drives the Plex PIN sign-in flow: start a challenge, open the Plex auth popup, then poll until
 * the user authorises (→ `onToken`), the backend rejects them, the challenge expires, or we hit the
 * timeout cap. The interval is always cleared on unmount and on any terminal state — no runaway
 * polling.
 *
 * We deliberately do NOT stop polling when the popup looks closed: once it navigates to plex.tv
 * (cross-origin, COOP), `window.closed` is unreliable and can read `true` while the popup is still
 * open — and a user who authorises then closes the "you're all set" tab would otherwise lose their
 * token. The timeout is the backstop for an abandoned attempt instead. */

import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError, api } from "../api";
import { logger } from "../logger";

const POLL_INTERVAL_MS = 2_000;
/** Cap polling at ~2.5 min so a never-authorised challenge can't poll forever. */
const POLL_TIMEOUT_MS = 150_000;

export type PlexStatus = "idle" | "pending" | "expired" | "timeout" | "denied" | "error";

interface PlexSignIn {
  status: PlexStatus;
  pending: boolean;
  error: string | null;
  start: () => void;
}

export function usePlexSignIn(onToken: (token: string) => void): PlexSignIn {
  const [status, setStatus] = useState<PlexStatus>("idle");
  const [error, setError] = useState<string | null>(null);
  const intervalRef = useRef<number | null>(null);
  const popupRef = useRef<Window | null>(null);
  const onTokenRef = useRef(onToken);
  onTokenRef.current = onToken;

  const stop = useCallback(() => {
    if (intervalRef.current !== null) {
      window.clearInterval(intervalRef.current);
      intervalRef.current = null;
    }
  }, []);

  const closePopup = useCallback(() => {
    try {
      popupRef.current?.close();
    } catch {
      // Cross-origin popup we can't close — harmless, the user can close it.
    }
    popupRef.current = null;
  }, []);

  // Always clear the interval when the component using the hook unmounts.
  useEffect(() => stop, [stop]);

  const start = useCallback(() => {
    stop();
    setError(null);
    setStatus("pending");
    api
      .plexStart()
      .then(({ challengeId, authUrl }) => {
        popupRef.current = window.open(authUrl, "plex-auth", "width=600,height=700");
        const deadline = Date.now() + POLL_TIMEOUT_MS;
        intervalRef.current = window.setInterval(() => {
          if (Date.now() > deadline) {
            stop();
            setStatus("timeout");
            setError("timeout");
            return;
          }
          api
            .plexPoll(challengeId)
            .then((poll) => {
              if (poll.status === "authorized" && poll.token !== null) {
                stop();
                closePopup();
                setStatus("idle");
                onTokenRef.current(poll.token);
              } else if (poll.status === "expired") {
                stop();
                setStatus("expired");
                setError("expired");
              }
            })
            .catch((e: unknown) => {
              // A 4xx is terminal (e.g. the Plex account isn't a member of a bound server): stop and
              // surface it rather than spin to timeout. Transient errors just keep polling.
              if (e instanceof ApiError && e.status >= 400 && e.status < 500) {
                stop();
                setStatus("denied");
                setError(e.message);
                return;
              }
              logger.warn("plex.poll.error", { error: e instanceof Error ? e.message : String(e) });
            });
        }, POLL_INTERVAL_MS);
      })
      .catch((e: unknown) => {
        stop();
        setStatus("error");
        setError(e instanceof Error ? e.message : String(e));
      });
  }, [stop, closePopup]);

  return { status, pending: status === "pending", error, start };
}
