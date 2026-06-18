import { useMutation } from "@tanstack/react-query";
import { useState } from "react";
import { type ChatReply, type RecommendationItem, api } from "../api";
import { useProfileId } from "../app/ProfileContext";
import styles from "./routes.module.css";

type Turn = { role: "user" | "agent"; text: string; items?: RecommendationItem[] };

function tint(seed: string): string {
  let hash = 0;
  for (let i = 0; i < seed.length; i++) {
    hash = (hash * 31 + seed.charCodeAt(i)) % 360;
  }
  return `hsl(${hash} 38% 32%)`;
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
    chat.mutate(trimmed);
  }

  return (
    <div className={styles.page} data-testid="chat">
      <h1 className={styles.pageTitle}>Chat</h1>
      <p className="muted">Tell the agent your mood — "tired, something funny under 90 minutes".</p>

      <div className={styles.chatLog}>
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
                  <div key={item.titleId} data-testid="chat-item">
                    <div
                      style={{
                        aspectRatio: "2 / 3",
                        borderRadius: "var(--radius-sm)",
                        background: tint(item.titleId),
                        marginBottom: "0.25rem",
                      }}
                    />
                    <div style={{ fontSize: "0.75rem", fontWeight: 500 }}>{item.title}</div>
                  </div>
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
