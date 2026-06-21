/** Drives the Plex PIN sign-in flow: start a challenge, open the Plex auth popup, then poll until
 * the user authorises (→ `onToken`), the challenge expires, or we hit the timeout cap. The interval
 * is always cleared on unmount, on a closed popup, and on any terminal state — no runaway polling. */

import { useCallback, useEffect, useRef, useState } from "react";
import { api } from "../api";
import { logger } from "../logger";

const POLL_INTERVAL_MS = 2_000;
/** Cap polling at ~2.5 min so a never-authorised challenge can't poll forever. */
const POLL_TIMEOUT_MS = 150_000;

export type PlexStatus = "idle" | "pending" | "expired" | "timeout" | "error";

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
          if (popupRef.current?.closed === true) {
            stop();
            setStatus("idle");
            return;
          }
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
                setStatus("idle");
                onTokenRef.current(poll.token);
              } else if (poll.status === "expired") {
                stop();
                setStatus("expired");
                setError("expired");
              }
            })
            .catch((e: unknown) => {
              logger.warn("plex.poll.error", { error: e instanceof Error ? e.message : String(e) });
            });
        }, POLL_INTERVAL_MS);
      })
      .catch((e: unknown) => {
        stop();
        setStatus("error");
        setError(e instanceof Error ? e.message : String(e));
      });
  }, [stop]);

  return { status, pending: status === "pending", error, start };
}
