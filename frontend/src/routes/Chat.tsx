import { useState } from "react";
import { useTranslation } from "react-i18next";
import { type RecommendationItem, api } from "../api";
import { type ChatTurn, useChat } from "../app/ChatContext";
import { useProfileId } from "../app/ProfileContext";
import { TitleDetailSheet } from "../components/TitleDetailSheet";
import { posterTint } from "../lib/poster";
import { useChatOpening, useInvalidateAfterChat, useUndoChatAction } from "../lib/queries";
import styles from "./routes.module.css";

/** Suggestion keys map to a translated chip label; "whyThese" also rewrites the outbound message. */
const STARTER_KEYS = ["funnyShort", "slowBurnSciFi", "comfortRewatch"] as const;
const FOLLOWUP_KEYS = ["weirder", "shorter", "lighter", "whyThese"] as const;

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

function ChatPoster({ item }: { item: RecommendationItem }): React.JSX.Element {
  const [failed, setFailed] = useState(false);
  const [detailOpen, setDetailOpen] = useState(false);
  const show = item.posterUrl !== null && !failed;
  return (
    <>
      <button
        type="button"
        className={styles.chatItem}
        data-testid="chat-item"
        onClick={() => setDetailOpen(true)}
      >
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
    setLog((l) => [
      ...l,
      { role: "user", text: trimmed },
      { role: "agent", text: "", streaming: true, status: t("status.thinking") },
    ]);
    setMessage("");
    setPending(true);
    let wrote = false;
    try {
      await api.chatStream(profileId, outbound ?? trimmed, {
        onStatus: (label) => setLog((l) => patchLast(l, { status: label })),
        onMeta: (meta) => {
          wrote = meta.actions.length > 0;
          setLog((l) =>
            patchLast(l, { items: meta.items, actions: meta.actions, degraded: meta.degraded }),
          );
        },
        onDelta: (chunk) => setLog((l) => patchLast(l, (t) => ({ text: t.text + chunk }))),
        onDone: () => setLog((l) => patchLast(l, { streaming: false })),
      });
    } catch {
      setLog((l) => patchLast(l, { text: t("error"), streaming: false }));
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
              <div className={styles.bubbleStrip}>
                {turn.items.map((item) => (
                  <ChatPoster key={item.titleId} item={item} />
                ))}
              </div>
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
