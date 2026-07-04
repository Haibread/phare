import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { ApiError, type RecommendationItem, api } from "../api";
import { type ChatTurn, useChat } from "../app/ChatContext";
import { useProfileId } from "../app/ProfileContext";
import { TitleDetailSheet } from "../components/TitleDetailSheet";
import { posterTint } from "../lib/poster";
import { useChatOpening, useInvalidateAfterChat, useUndoChatAction } from "../lib/queries";
import styles from "./routes.module.css";

/** Suggestion keys map to a translated chip label; "whyThese" also rewrites the outbound message. */
const STARTER_KEYS = ["funnyShort", "slowBurnSciFi", "comfortRewatch"] as const;
const FOLLOWUP_KEYS = ["weirder", "shorter", "lighter", "whyThese"] as const;

/** Escape a string for safe inclusion in a RegExp — titles carry `()+.?` etc. and this is a display
 * heuristic that must never throw on an odd title. */
function escapeRegExp(s: string): string {
  return s.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/** Which of `items` the agent actually named in `reply`. Match is case-insensitive on the title as a
 * whole "phrase" bounded by non-word characters, so a short/generic title ("It", "Us") doesn't match
 * mid-word (e.g. "It" inside "with"). Purely a display heuristic — resilient to any title text. */
export function citedTitleIds(reply: string, items: readonly RecommendationItem[]): Set<string> {
  const cited = new Set<string>();
  if (reply.trim() === "") {
    return cited;
  }
  for (const item of items) {
    const title = item.title.trim();
    if (title === "") {
      continue;
    }
    // \b is unreliable around punctuation/unicode; bound on start/whitespace/non-word instead.
    const re = new RegExp(`(^|[^\\p{L}\\p{N}])${escapeRegExp(title)}($|[^\\p{L}\\p{N}])`, "iu");
    if (re.test(reply)) {
      cited.add(item.titleId);
    }
  }
  return cited;
}

/** Cited picks float to the front (stable order otherwise), so the titles the reply argues for lead
 * the grid instead of drowning in it. No citation → the returned order is untouched. */
export function orderByCitation(
  items: readonly RecommendationItem[],
  cited: Set<string>,
): RecommendationItem[] {
  if (cited.size === 0) {
    return [...items];
  }
  return [
    ...items.filter((i) => cited.has(i.titleId)),
    ...items.filter((i) => !cited.has(i.titleId)),
  ];
}

/** Apply a patch to the last turn (the streaming agent bubble) without mutating the array. */
function patchLast(
  log: ChatTurn[],
  patch: Partial<ChatTurn> | ((turn: ChatTurn) => Partial<ChatTurn>),
): ChatTurn[] {
  const last = log[log.length - 1];
  if (last === undefined) {
    return log;
  }
  const delta = typeof patch === "function" ? patch(last) : patch;
  return [...log.slice(0, -1), { ...last, ...delta }];
}

function ChatPoster({
  item,
  cited = false,
}: {
  item: RecommendationItem;
  cited?: boolean;
}): React.JSX.Element {
  const { t } = useTranslation("chat");
  const [failed, setFailed] = useState(false);
  const [detailOpen, setDetailOpen] = useState(false);
  const show = item.posterUrl !== null && !failed;
  return (
    <>
      <button
        type="button"
        className={`${styles.chatItem} ${cited ? styles.chatItemCited : ""}`}
        data-testid={cited ? "chat-item-cited" : "chat-item"}
        onClick={() => setDetailOpen(true)}
      >
        {cited && (
          <span className={styles.chatCitedTag} data-testid="chat-cited-tag">
            {t("recommended")}
          </span>
        )}
        <div
          className={styles.chatPoster}
          style={show ? undefined : { background: posterTint(item.titleId) }}
        >
          {show && (
            <img src={item.posterUrl ?? ""} alt="" loading="lazy" onError={() => setFailed(true)} />
          )}
        </div>
        <div style={{ fontSize: "0.75rem", fontWeight: 500 }}>{item.title}</div>
      </button>
      <TitleDetailSheet item={item} open={detailOpen} onOpenChange={setDetailOpen} />
    </>
  );
}

/** The grid of a turn's returned picks. Once the reply text is settled, the titles it argues for by
 * name lead the grid and wear a "recommended" tag; while streaming (empty reply) nothing is cited so
 * the grid renders in its returned order. Recomputed only when the reply or items change. */
function ChatItemGrid({
  items,
  reply,
}: {
  items: readonly RecommendationItem[];
  reply: string;
}): React.JSX.Element {
  const { cited, ordered } = useMemo(() => {
    const c = citedTitleIds(reply, items);
    return { cited: c, ordered: orderByCitation(items, c) };
  }, [reply, items]);
  return (
    <div className={styles.bubbleStrip} data-testid="chat-item-grid">
      {ordered.map((item) => (
        <ChatPoster key={item.titleId} item={item} cited={cited.has(item.titleId)} />
      ))}
    </div>
  );
}

export function Chat(): React.JSX.Element {
  const { t } = useTranslation("chat");
  const profileId = useProfileId();
  const { log, setLog, undone, setUndone, reset } = useChat();
  const [message, setMessage] = useState("");
  const [pending, setPending] = useState(false);
  const opening = useChatOpening(profileId);
  const undo = useUndoChatAction(profileId);
  const invalidateAfterChat = useInvalidateAfterChat(profileId);

  function undoAction(token: string) {
    undo.mutate(token, { onSuccess: () => setUndone((s) => new Set(s).add(token)) });
  }

  /** `outbound` overrides the message sent to the agent (e.g. the "why these?" chip). */
  async function send(text: string, outbound?: string) {
    const trimmed = text.trim();
    if (trimmed === "" || pending) {
      return;
    }
    // The conversation so far (this turn's message isn't appended yet) — replayed so the agent can
    // resolve references and not repeat itself. The backend bounds it; we just drop empty bubbles.
    const history = log
      .filter((turn) => turn.text.trim() !== "")
      .map((turn) => ({ role: turn.role, text: turn.text }));
    // The filters in effect from the most recent turn that actually recommended, so "even shorter"
    // can tighten them. Turns that only asked a clarifying question or declined carry a throwaway
    // keyword-parsed intent and no picks — skip those so they don't pollute the active filters.
    const activeIntent =
      [...log]
        .reverse()
        .find((turn) => turn.role === "agent" && turn.intent && (turn.items?.length ?? 0) > 0)
        ?.intent ?? null;
    setLog((l) => [
      ...l,
      { role: "user", text: trimmed },
      { role: "agent", text: "", streaming: true, status: t("status.thinking") },
    ]);
    setMessage("");
    setPending(true);
    let wrote = false;
    try {
      await api.chatStream(
        profileId,
        outbound ?? trimmed,
        { history, activeIntent },
        {
          onStatus: (label) => setLog((l) => patchLast(l, { status: label })),
          onMeta: (meta) => {
            wrote = meta.actions.length > 0;
            setLog((l) =>
              patchLast(l, {
                items: meta.items,
                actions: meta.actions,
                degraded: meta.degraded,
                intent: meta.intent,
                suggestions: meta.suggestions,
              }),
            );
          },
          onDelta: (chunk) => setLog((l) => patchLast(l, (t) => ({ text: t.text + chunk }))),
          onDone: () => setLog((l) => patchLast(l, { streaming: false })),
        },
      );
    } catch (e) {
      // A 429 (rate limited) gets its own message so the user knows to wait, not that it "failed".
      const key = e instanceof ApiError && e.status === 429 ? "rateLimited" : "error";
      setLog((l) => patchLast(l, { text: t(key), streaming: false }));
    } finally {
      setPending(false);
      if (wrote) {
        invalidateAfterChat(); // a chat write (e.g. "loved X") should refresh Browse + Profile
      }
    }
  }

  const suggestionKeys = log.length === 0 ? STARTER_KEYS : FOLLOWUP_KEYS;

  return (
    <div className={styles.page} data-testid="chat">
      <div className={styles.chatHead}>
        <h1 className={styles.pageTitle}>{t("title")}</h1>
        {log.length > 0 && (
          <button
            type="button"
            className={`btn ${styles.newChat}`}
            data-testid="chat-new"
            onClick={reset}
            disabled={pending}
          >
            {t("newChat")}
          </button>
        )}
      </div>

      <div className={styles.chatLog} aria-live="polite" aria-label={t("conversation")}>
        {log.length === 0 && (
          <div className={styles.bubble} data-testid="chat-greeting">
            <p style={{ margin: 0 }}>{opening.data?.greeting ?? t("greeting")}</p>
          </div>
        )}
        {log.map((turn, i) => (
          <div
            // Chat turns are append-only; index is a stable key here.
            key={`${turn.role}-${i}`}
            className={`${styles.bubble} ${turn.role === "user" ? styles.bubbleUser : ""}`}
            data-testid={`chat-${turn.role}`}
          >
            {turn.streaming && turn.text === "" ? (
              <span className="faint" data-testid="chat-status">
                {turn.status ?? t("status.thinking")}
              </span>
            ) : (
              <p style={{ margin: 0 }}>
                {turn.text}
                {turn.streaming && <span className={styles.caret} aria-hidden="true" />}
              </p>
            )}
            {turn.degraded && !turn.streaming && (
              <p className={styles.reducedMode} data-testid="chat-degraded">
                {t("reducedMode")}
              </p>
            )}
            {turn.suggestions && turn.suggestions.length > 0 && !turn.streaming && (
              <div className={styles.followups} data-testid="chat-suggestions">
                {turn.suggestions.map((suggestion) => (
                  <button
                    key={suggestion}
                    type="button"
                    className={styles.followup}
                    data-testid="chat-clarify-suggestion"
                    disabled={pending}
                    onClick={() => send(suggestion)}
                  >
                    {suggestion}
                  </button>
                ))}
              </div>
            )}
            {turn.actions && turn.actions.length > 0 && (
              <div className={styles.actionChips} data-testid="chat-actions">
                {turn.actions.map((action) => (
                  <span
                    key={action.summary}
                    className={styles.actionChip}
                    data-testid="chat-action"
                  >
                    ✓ {action.summary}
                    {action.undoToken &&
                      (undone.has(action.undoToken) ? (
                        <span className="faint"> · {t("undone")}</span>
                      ) : (
                        <button
                          type="button"
                          className={styles.undoBtn}
                          data-testid="chat-undo"
                          disabled={undo.isPending}
                          onClick={() => action.undoToken && undoAction(action.undoToken)}
                        >
                          {t("undo")}
                        </button>
                      ))}
                  </span>
                ))}
              </div>
            )}
            {turn.items && turn.items.length > 0 && (
              <ChatItemGrid items={turn.items} reply={turn.streaming ? "" : turn.text} />
            )}
          </div>
        ))}
      </div>

      {!pending && (
        <div className={styles.followups}>
          {suggestionKeys.map((key) => {
            const label = t(`suggestions.${key}`);
            return (
              <button
                key={key}
                type="button"
                className={styles.followup}
                data-testid="chat-suggestion"
                onClick={() =>
                  send(label, key === "whyThese" ? t("suggestions.whyThesePayload") : undefined)
                }
              >
                {label}
              </button>
            );
          })}
        </div>
      )}

      <form
        className={styles.composer}
        onSubmit={(e) => {
          e.preventDefault();
          send(message);
        }}
      >
        <label htmlFor="chat-input" className="sr-only">
          {t("inputLabel")}
        </label>
        <input
          id="chat-input"
          type="text"
          className="field"
          data-testid="chat-input"
          placeholder={t("inputPlaceholder")}
          value={message}
          onChange={(e) => setMessage(e.target.value)}
        />
        <button
          type="submit"
          className="btn btn-primary"
          data-testid="chat-send"
          disabled={pending || message.trim() === ""}
        >
          {t("send")}
        </button>
      </form>
    </div>
  );
}
