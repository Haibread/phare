import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import { type ChatReply, type RecommendationItem, api } from "../api";
import { useProfileId } from "../app/ProfileContext";
import { posterTint } from "../lib/poster";
import styles from "./routes.module.css";

type Turn = { role: "user" | "agent"; text: string; items?: RecommendationItem[] };

const STARTERS = ["something funny and short", "a slow-burn sci-fi", "a comfort rewatch"];
const FOLLOWUPS = ["something weirder", "even shorter", "lighter", "why these?"];

function ChatPoster({ item }: { item: RecommendationItem }): React.JSX.Element {
  const [failed, setFailed] = useState(false);
  const show = item.posterUrl !== null && !failed;
  return (
    <div data-testid="chat-item">
      <div
        className={styles.chatPoster}
        style={show ? undefined : { background: posterTint(item.titleId) }}
      >
        {show && (
          <img src={item.posterUrl ?? ""} alt="" loading="lazy" onError={() => setFailed(true)} />
        )}
      </div>
      <div style={{ fontSize: "0.75rem", fontWeight: 500 }}>{item.title}</div>
    </div>
  );
}

export function Chat(): React.JSX.Element {
  const profileId = useProfileId();
  const [log, setLog] = useState<Turn[]>([]);
  const [message, setMessage] = useState("");

  const chat = useMutation({
    mutationFn: (text: string) => api.chat(profileId, text),
    onSuccess: (reply: ChatReply) =>
      setLog((l) => [...l, { role: "agent", text: reply.replyText, items: reply.items }]),
  });

  function send(text: string) {
    const trimmed = text.trim();
    if (trimmed === "") {
      return;
    }
    setLog((l) => [...l, { role: "user", text: trimmed }]);
    setMessage("");
    chat.mutate(trimmed === "why these?" ? "why did you pick these?" : trimmed);
  }

  const suggestions = log.length === 0 ? STARTERS : FOLLOWUPS;

  return (
    <div className={styles.page} data-testid="chat">
      <h1 className={styles.pageTitle}>Chat</h1>

      <div className={styles.chatLog}>
        {log.length === 0 && (
          <div className={styles.bubble} data-testid="chat-greeting">
            <p style={{ margin: 0 }}>
              Tell me your mood and I'll find something — "tired, something funny under 90 minutes".
            </p>
          </div>
        )}
        {log.map((turn, i) => (
          <div
            // Chat turns are append-only; index is a stable key here.
            key={`${turn.role}-${i}`}
            className={`${styles.bubble} ${turn.role === "user" ? styles.bubbleUser : ""}`}
            data-testid={`chat-${turn.role}`}
          >
            <p style={{ margin: 0 }}>{turn.text}</p>
            {turn.items && turn.items.length > 0 && (
              <div className={styles.bubbleStrip}>
                {turn.items.map((item) => (
                  <ChatPoster key={item.titleId} item={item} />
                ))}
              </div>
            )}
          </div>
        ))}
        {chat.isPending && (
          <div className={styles.bubble} data-testid="chat-pending">
            <span className="faint">Thinking…</span>
          </div>
        )}
      </div>

      {!chat.isPending && (
        <div className={styles.followups}>
          {suggestions.map((p) => (
            <button
              key={p}
              type="button"
              className={styles.followup}
              data-testid="chat-suggestion"
              onClick={() => send(p)}
            >
              {p}
            </button>
          ))}
        </div>
      )}

      <div className={styles.composer}>
        <input
          type="text"
          className="field"
          data-testid="chat-input"
          placeholder="e.g. something funny and short"
          value={message}
          onChange={(e) => setMessage(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && send(message)}
        />
        <button
          type="button"
          className="btn btn-primary"
          data-testid="chat-send"
          onClick={() => send(message)}
          disabled={chat.isPending || message.trim() === ""}
        >
          Send
        </button>
      </div>
    </div>
  );
}
