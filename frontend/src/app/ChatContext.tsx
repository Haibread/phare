import {
  type Dispatch,
  type ReactNode,
  type SetStateAction,
  createContext,
  useContext,
  useEffect,
  useState,
} from "react";
import type { AgentAction, RecommendationItem } from "../api";

/** One message in the conversation. `streaming` marks the agent turn whose text is still arriving. */
export interface ChatTurn {
  role: "user" | "agent";
  text: string;
  items?: RecommendationItem[];
  actions?: AgentAction[];
  streaming?: boolean;
}

interface ChatState {
  log: ChatTurn[];
  setLog: Dispatch<SetStateAction<ChatTurn[]>>;
  undone: Set<string>;
  setUndone: Dispatch<SetStateAction<Set<string>>>;
  reset: () => void;
}

const ChatCtx = createContext<ChatState | null>(null);
const STORAGE_KEY = "phare.chat.log";

function loadLog(): ChatTurn[] {
  try {
    const raw = sessionStorage.getItem(STORAGE_KEY);
    return raw ? (JSON.parse(raw) as ChatTurn[]) : [];
  } catch {
    return [];
  }
}

/** Holds the conversation above the routed screens, so it survives tab switches (the provider is
 * mounted in AppShell, which stays mounted) and a reload (mirrored to sessionStorage). */
export function ChatProvider({ children }: { children: ReactNode }): React.JSX.Element {
  const [log, setLog] = useState<ChatTurn[]>(loadLog);
  const [undone, setUndone] = useState<Set<string>>(new Set());

  useEffect(() => {
    // A turn mid-stream isn't worth persisting; store the settled conversation.
    if (!log.some((t) => t.streaming)) {
      sessionStorage.setItem(STORAGE_KEY, JSON.stringify(log));
    }
  }, [log]);

  function reset() {
    setLog([]);
    setUndone(new Set());
    sessionStorage.removeItem(STORAGE_KEY);
  }

  return (
    <ChatCtx.Provider value={{ log, setLog, undone, setUndone, reset }}>
      {children}
    </ChatCtx.Provider>
  );
}

export function useChat(): ChatState {
  const ctx = useContext(ChatCtx);
  if (ctx === null) {
    throw new Error("useChat must be used within a ChatProvider");
  }
  return ctx;
}
