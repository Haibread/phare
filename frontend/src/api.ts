/** Typed API client. All responses are validated with zod at the I/O boundary. */

import { z } from "zod";
import { logger } from "./logger";

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";

/** An HTTP error from the backend, carrying the status so callers can distinguish a terminal
 * rejection (4xx) from a transient blip worth retrying. Network/parse failures stay plain `Error`. */
export class ApiError extends Error {
  readonly status: number;
  constructor(status: number, detail: string, options?: ErrorOptions) {
    super(detail, options);
    this.name = "ApiError";
    this.status = status;
  }
  /** Status 0 = the request never reached the server (network/CORS/opaque failure). */
  get isNetwork(): boolean {
    return this.status === 0;
  }
}

export const profileSchema = z.object({
  id: z.string(),
  displayName: z.string(),
  createdAt: z.string(),
});
export type Profile = z.infer<typeof profileSchema>;

const profilePageSchema = z.object({
  items: z.array(profileSchema),
  page: z.number(),
  perPage: z.number(),
  total: z.number(),
});

export const historyItemSchema = z.object({
  id: z.string(),
  titleId: z.string(),
  title: z.string(),
  kind: z.string(),
  type: z.string(),
  rating: z.number().nullable(),
  occurredAt: z.string().nullable(),
  seasonNumber: z.number().nullable(),
  episodeNumber: z.number().nullable(),
  source: z.string(),
  excluded: z.boolean(),
});
export type HistoryItem = z.infer<typeof historyItemSchema>;

const historyPageSchema = z.object({
  items: z.array(historyItemSchema),
  page: z.number(),
  perPage: z.number(),
  total: z.number(),
});

const ingestSummarySchema = z.object({
  created: z.number(),
  updated: z.number(),
  skipped: z.number(),
  titlesCreated: z.number(),
});
export type IngestSummary = z.infer<typeof ingestSummarySchema>;

export const tasteSchema = z.object({
  profileId: z.string(),
  summary: z.string().nullable(),
  structured: z.record(z.unknown()),
  userOverrides: z.record(z.unknown()),
  confidence: z.number().nullable(),
  modelVersion: z.string().nullable(),
  generatedAt: z.string().nullable(),
});
export type Taste = z.infer<typeof tasteSchema>;

export const recommendationItemSchema = z.object({
  titleId: z.string(),
  title: z.string(),
  kind: z.string(),
  year: z.number().nullable(),
  genres: z.array(z.string()),
  score: z.number(),
  isSwing: z.boolean(),
  confidence: z.number().nullable(),
  explanation: z.string().nullable(),
  posterUrl: z.string().nullable(),
  components: z.record(z.number()),
  watched: z.boolean(),
});
export type RecommendationItem = z.infer<typeof recommendationItemSchema>;

export const titleDetailSchema = z.object({
  titleId: z.string(),
  title: z.string(),
  kind: z.string(),
  year: z.number().nullable(),
  runtimeMinutes: z.number().nullable(),
  genres: z.array(z.string()),
  overview: z.string().nullable(),
  posterUrl: z.string().nullable(),
  tmdbUrl: z.string().nullable(),
  imdbUrl: z.string().nullable(),
});
export type TitleDetail = z.infer<typeof titleDetailSchema>;

export const recommendationRowSchema = z.object({
  key: z.string(),
  title: z.string(),
  items: z.array(recommendationItemSchema),
});
export type RecommendationRow = z.infer<typeof recommendationRowSchema>;

const recommendationsResponseSchema = z.object({
  rows: z.array(recommendationRowSchema),
  // True when a configured LLM couldn't name today's themed rows and the deterministic fallback
  // filled in — the UI flags "basic picks" instead of passing them off as AI-curated.
  degraded: z.boolean().default(false),
  // True when retrieval runs on the local hash embedder (no embedding key): similarity is not
  // semantically meaningful, so the UI shows an honest banner and caps the fit label.
  embeddingsDegraded: z.boolean().default(false),
  // True when the profile has history but its taste centroid isn't ready yet (titles still
  // embedding) — the UI shows a "building your profile" note instead of a bare page.
  profileBuilding: z.boolean().default(false),
});

const chatIntentSchema = z.object({
  maxRuntime: z.number().nullable(),
  includeGenres: z.array(z.string()),
  excludeGenres: z.array(z.string()),
  mood: z.string().nullable(),
});
/** The filters in effect after a turn — replayed on the next so a follow-up refines, not restarts. */
export type ChatIntent = z.infer<typeof chatIntentSchema>;

export const agentActionSchema = z.object({
  kind: z.string(),
  summary: z.string(),
  undoToken: z.string().nullable(),
});
export type AgentAction = z.infer<typeof agentActionSchema>;

export const chatReplySchema = z.object({
  replyText: z.string(),
  intent: chatIntentSchema,
  items: z.array(recommendationItemSchema),
  actions: z.array(agentActionSchema),
  suggestions: z.array(z.string()).default([]),
  // The AI couldn't fully process the turn (planner output unparseable) and fell back to a plain
  // recommendation — no writes, no mood parsing. The UI flags it instead of implying it understood.
  degraded: z.boolean().default(false),
});
export type ChatReply = z.infer<typeof chatReplySchema>;

// The first SSE event of a streamed turn: the picks + writes, before the reply text streams in.
export const chatStreamMetaSchema = z.object({
  intent: chatIntentSchema,
  items: z.array(recommendationItemSchema),
  actions: z.array(agentActionSchema),
  // Tappable quick-replies when the agent asked a clarifying question instead of recommending.
  suggestions: z.array(z.string()).default([]),
  degraded: z.boolean().default(false),
});
export type ChatStreamMeta = z.infer<typeof chatStreamMetaSchema>;

export interface ChatStreamHandlers {
  onStatus?: (label: string) => void;
  onMeta?: (meta: ChatStreamMeta) => void;
  onDelta?: (text: string) => void;
  onDone?: () => void;
}

/** A prior turn replayed to the agent so it can resolve references and not repeat itself. */
export type ChatHistoryMessage = { role: "user" | "agent"; text: string };

/** The conversation context a chat turn carries: the recent transcript + the filters in effect. */
export interface ChatStreamOptions {
  history: ChatHistoryMessage[];
  activeIntent: ChatIntent | null;
  signal?: AbortSignal;
}

const chatOpeningSchema = z.object({ greeting: z.string().nullable() });
const undoResultSchema = z.object({ undone: z.boolean() });

const catalogSummarySchema = z.object({ created: z.number() });

/** The only card-feedback signal for now — extensible, but deliberately no thumbs-up (K2). */
export type FeedbackSignal = "not_interested";
const feedbackResponseSchema = z.object({
  titleId: z.string(),
  signal: z.string(),
  // Reuse the chat undo mechanism: hand this to `undoChatAction` to reverse the signal.
  undoToken: z.string(),
});

export const traktConnectStartSchema = z.object({
  deviceCode: z.string(),
  userCode: z.string(),
  verificationUrl: z.string(),
  interval: z.number(),
  expiresIn: z.number(),
});
export type TraktConnectStart = z.infer<typeof traktConnectStartSchema>;

const traktConnectStatusSchema = z.object({ status: z.string() });

export const conversionSchema = z.object({
  shown: z.number(),
  converted: z.number(),
  rate: z.number().nullable(),
  swingShown: z.number(),
  swingConverted: z.number(),
  swingRate: z.number().nullable(),
  topK: z.number(),
  withinDays: z.number(),
});
export type Conversion = z.infer<typeof conversionSchema>;

export const userSchema = z.object({
  id: z.string(),
  email: z.string().nullable(),
  displayName: z.string(),
  isAdmin: z.boolean(),
  profileId: z.string(),
});
export type User = z.infer<typeof userSchema>;

const meSchema = z.object({
  needsSetup: z.boolean(),
  registrationOpen: z.boolean(),
  authenticated: z.boolean(),
  user: userSchema.nullable(),
});
export type Me = z.infer<typeof meSchema>;
const tokenSchema = z.object({ token: z.string() });

const registerResponseSchema = z.object({ token: z.string().nullable(), user: userSchema });
export type RegisterResponse = z.infer<typeof registerResponseSchema>;

const plexStartSchema = z.object({ challengeId: z.string(), authUrl: z.string() });
export type PlexStart = z.infer<typeof plexStartSchema>;

const plexPollSchema = z.object({
  status: z.enum(["pending", "authorized", "expired"]),
  token: z.string().nullable(),
});
export type PlexPoll = z.infer<typeof plexPollSchema>;

export const connectedSourceSchema = z.object({
  source: z.string(),
  kind: z.string(),
  lastSyncedAt: z.string().nullable(),
});
export type ConnectedSource = z.infer<typeof connectedSourceSchema>;

// Seerr library availability per title: "available" | "queued" | "requestable" | "unknown".
const availabilitySchema = z.object({
  configured: z.boolean(),
  results: z.record(z.string()),
});
export type Availability = z.infer<typeof availabilitySchema>;
const requestResultSchema = z.object({ ok: z.boolean(), availability: z.string() });
const connectedSchema = z.object({ connected: z.boolean() });

export const commitmentSchema = z.object({
  id: z.string(),
  titleId: z.string(),
  title: z.string(),
  kind: z.string(),
  posterUrl: z.string().nullable(),
  status: z.string(),
  note: z.string().nullable(),
  createdAt: z.string(),
  resolvedAt: z.string().nullable(),
});
export type Commitment = z.infer<typeof commitmentSchema>;
const commitmentListSchema = z.object({ items: z.array(commitmentSchema) });

export const memoryNoteSchema = z.object({
  id: z.string(),
  text: z.string(),
  kind: z.string(),
  expiresAt: z.string().nullable(),
  source: z.string(),
  createdAt: z.string(),
});
export type MemoryNote = z.infer<typeof memoryNoteSchema>;
const memoryNoteListSchema = z.object({ items: z.array(memoryNoteSchema) });

// Bearer token, persisted to localStorage so a reload keeps you signed in. It's a short-lived
// bearer (30-day TTL, see docs/auth.md); for a self-hosted instance that's the right trade-off —
// re-authenticating on every refresh is worse UX than the marginal XSS exposure. Mirrored in a
// module variable to avoid a storage read on every request, and dropped on a 401 (expiry).
const TOKEN_STORAGE_KEY = "phare.token";

function readStoredToken(): string | null {
  try {
    return localStorage.getItem(TOKEN_STORAGE_KEY);
  } catch {
    return null; // storage unavailable (private mode) — fall back to in-memory only
  }
}

let authToken: string | null = readStoredToken();

export function setAuthToken(token: string | null): void {
  authToken = token;
  try {
    if (token === null) {
      localStorage.removeItem(TOKEN_STORAGE_KEY);
    } else {
      localStorage.setItem(TOKEN_STORAGE_KEY, token);
    }
  } catch {
    // storage unavailable — keep the in-memory token so the current session still works
  }
}

// Active UI language, sent as Accept-Language so the backend can localise the text it generates
// (TMDB metadata, LLM explanations/chat). Kept in sync with i18next by src/i18n.ts.
let apiLanguage = "en";
export function setApiLanguage(language: string): void {
  apiLanguage = language;
}

async function request<T>(path: string, schema: z.ZodType<T>, init?: RequestInit): Promise<T> {
  const url = `${API_BASE}${path}`;
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    "Accept-Language": apiLanguage,
  };
  if (authToken !== null) {
    headers.Authorization = `Bearer ${authToken}`;
  }
  let response: Response;
  try {
    response = await fetch(url, {
      ...init,
      headers: { ...headers, ...(init?.headers as Record<string, string> | undefined) },
    });
  } catch (cause) {
    // fetch() itself rejected — the server is unreachable, or a 500 came back without CORS headers
    // (the browser surfaces both as an opaque "Failed to fetch"). Throw a typed network error so the
    // UI can show a friendly, localised message instead of leaking the raw TypeError.
    logger.warn("api.network_error", { url });
    throw new ApiError(0, "network", { cause });
  }
  if (!response.ok) {
    if (response.status === 401) {
      // Expired/invalid token — drop it so the app falls back to the login gate on the next /me.
      setAuthToken(null);
    }
    let detail = response.statusText;
    try {
      const body: unknown = await response.json();
      if (body && typeof body === "object" && "detail" in body) {
        detail = String((body as { detail: unknown }).detail);
      }
    } catch {
      // non-JSON error body; keep the status text
    }
    logger.warn("api.error", { url, status: response.status, detail });
    throw new ApiError(response.status, detail);
  }
  logger.debug("api.ok", { url, status: response.status });
  // 204 No Content (e.g. DELETE) has no body to parse — validate against null.
  if (response.status === 204) {
    return schema.parse(null);
  }
  return schema.parse(await response.json());
}

/** Consume a Server-Sent Events response, dispatching each `(event, parsed-data)` to `onEvent`. */
async function readEventStream(
  response: Response,
  onEvent: (event: string, data: unknown) => void,
): Promise<void> {
  if (!response.ok || response.body === null) {
    throw new Error(response.statusText || "stream failed");
  }
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) {
      break;
    }
    buffer += decoder.decode(value, { stream: true });
    let sep = buffer.indexOf("\n\n");
    while (sep >= 0) {
      const block = buffer.slice(0, sep);
      buffer = buffer.slice(sep + 2);
      let event = "message";
      let data = "";
      for (const line of block.split("\n")) {
        if (line.startsWith("event:")) {
          event = line.slice(6).trim();
        } else if (line.startsWith("data:")) {
          data += line.slice(5).trim();
        }
      }
      if (data !== "") {
        onEvent(event, JSON.parse(data));
      }
      sep = buffer.indexOf("\n\n");
    }
  }
}

function authHeaders(extra?: Record<string, string>): Record<string, string> {
  const headers: Record<string, string> = { "Accept-Language": apiLanguage, ...extra };
  if (authToken !== null) {
    headers.Authorization = `Bearer ${authToken}`;
  }
  return headers;
}

/** Stream a chat turn over Server-Sent Events: a `status` line, then the picks/writes in one `meta`
 * event, then the reply text as `delta` events. Resolves when the stream closes (`done`). */
async function chatStream(
  profileId: string,
  message: string,
  options: ChatStreamOptions,
  handlers: ChatStreamHandlers,
): Promise<void> {
  const { history, activeIntent, signal } = options;
  const response = await fetch(`${API_BASE}/profiles/${profileId}/chat/stream`, {
    method: "POST",
    headers: authHeaders({ "Content-Type": "application/json" }),
    body: JSON.stringify({ message, history, activeIntent }),
    signal: signal ?? null,
  });
  await readEventStream(response, (event, parsed) => {
    if (event === "status") {
      handlers.onStatus?.(String((parsed as { label: string }).label));
    } else if (event === "meta") {
      handlers.onMeta?.(chatStreamMetaSchema.parse(parsed));
    } else if (event === "delta") {
      handlers.onDelta?.(String((parsed as { text: string }).text));
    } else if (event === "done") {
      handlers.onDone?.();
    }
  });
}

/** Stream a title's lazily-generated "why this fits you" reason as `delta` chunks, then `done`. */
async function streamTitleExplanation(
  profileId: string,
  titleId: string,
  handlers: { onDelta?: (text: string) => void; onDone?: () => void },
  signal?: AbortSignal,
  // The "because you watched X" seed title id, when the card was opened from such a row — sharpens
  // the reason to that concrete link instead of the abstract taste.
  because?: string | null,
): Promise<void> {
  const query = because ? `?because=${encodeURIComponent(because)}` : "";
  const response = await fetch(
    `${API_BASE}/profiles/${profileId}/titles/${titleId}/explanation${query}`,
    {
      headers: authHeaders(),
      signal: signal ?? null,
    },
  );
  await readEventStream(response, (event, parsed) => {
    if (event === "delta") {
      handlers.onDelta?.(String((parsed as { text: string }).text));
    } else if (event === "done") {
      handlers.onDone?.();
    }
  });
}

export const api = {
  me: () => request("/me", meSchema),
  login: (email: string, password: string) =>
    request("/auth/login", tokenSchema, {
      method: "POST",
      body: JSON.stringify({ email, password }),
    }),
  register: (email: string, password: string, displayName: string) =>
    request("/auth/register", registerResponseSchema, {
      method: "POST",
      body: JSON.stringify({ email, password, displayName }),
    }),
  plexStart: () => request("/auth/plex/start", plexStartSchema, { method: "POST" }),
  plexPoll: (challengeId: string) =>
    request("/auth/plex/poll", plexPollSchema, {
      method: "POST",
      body: JSON.stringify({ challengeId }),
    }),
  listProfiles: () => request("/profiles", profilePageSchema),
  loadSampleData: (profileId: string) =>
    request(`/profiles/${profileId}/sample-data`, ingestSummarySchema, { method: "POST" }),
  history: (profileId: string) =>
    request(`/history?profileId=${profileId}&perPage=100`, historyPageSchema),
  syncTrakt: (profileId: string, accessToken?: string) =>
    request("/sources/trakt/sync", ingestSummarySchema, {
      method: "POST",
      // Omit the token when empty so the backend falls back to a stored (OAuth-connected) one.
      body: JSON.stringify(accessToken ? { profileId, accessToken } : { profileId }),
    }),
  listSources: (profileId: string) =>
    request(`/profiles/${profileId}/sources`, z.array(connectedSourceSchema)),
  syncPlex: (profileId: string, baseUrl: string, token: string) =>
    request("/sources/plex/sync", ingestSummarySchema, {
      method: "POST",
      body: JSON.stringify({ profileId, baseUrl, token }),
    }),
  syncJellyfin: (profileId: string, baseUrl: string, userId: string, apiKey: string) =>
    request("/sources/jellyfin/sync", ingestSummarySchema, {
      method: "POST",
      body: JSON.stringify({ profileId, baseUrl, userId, apiKey }),
    }),
  traktConnectStart: () =>
    request("/sources/trakt/connect/start", traktConnectStartSchema, { method: "POST" }),
  traktConnectPoll: (profileId: string, deviceCode: string) =>
    request("/sources/trakt/connect/poll", traktConnectStatusSchema, {
      method: "POST",
      body: JSON.stringify({ profileId, deviceCode }),
    }),
  getTaste: (profileId: string) => request(`/profiles/${profileId}/taste`, tasteSchema),
  generateTaste: (profileId: string) =>
    request(`/profiles/${profileId}/taste/generate`, tasteSchema, { method: "POST" }),
  updateTaste: (profileId: string, userOverrides: Record<string, unknown>) =>
    request(`/profiles/${profileId}/taste`, tasteSchema, {
      method: "PUT",
      body: JSON.stringify({ userOverrides }),
    }),
  seedCatalog: () => request("/catalog/sample", catalogSummarySchema, { method: "POST" }),
  recommendations: (profileId: string) =>
    request(`/profiles/${profileId}/recommendations`, recommendationsResponseSchema),
  dynamicRecommendations: (profileId: string) =>
    request(`/profiles/${profileId}/recommendations/dynamic`, recommendationsResponseSchema),
  conversion: (profileId: string) =>
    request(`/profiles/${profileId}/recommendations/conversion`, conversionSchema),
  titleDetail: (titleId: string) => request(`/titles/${titleId}`, titleDetailSchema),
  streamTitleExplanation,
  chat: (profileId: string, message: string) =>
    request(`/profiles/${profileId}/chat`, chatReplySchema, {
      method: "POST",
      body: JSON.stringify({ message }),
    }),
  chatStream: chatStream,
  chatOpening: (profileId: string) =>
    request(`/profiles/${profileId}/chat/opening`, chatOpeningSchema),
  undoChatAction: (profileId: string, token: string) =>
    request(`/profiles/${profileId}/chat/undo`, undoResultSchema, {
      method: "POST",
      body: JSON.stringify({ token }),
    }),
  sendTitleFeedback: (profileId: string, titleId: string, signal: FeedbackSignal) =>
    request(`/profiles/${profileId}/titles/${titleId}/feedback`, feedbackResponseSchema, {
      method: "POST",
      body: JSON.stringify({ signal }),
    }),
  searchCatalog: (profileId: string, q: string) =>
    request(
      `/profiles/${profileId}/catalog/search`,
      z.object({ results: z.array(recommendationItemSchema) }),
      { method: "POST", body: JSON.stringify({ q }) },
    ),
  availability: (profileId: string, titleIds: string[]) =>
    request(`/profiles/${profileId}/availability`, availabilitySchema, {
      method: "POST",
      body: JSON.stringify({ titleIds }),
    }),
  requestTitle: (profileId: string, titleId: string) =>
    request(`/profiles/${profileId}/requests`, requestResultSchema, {
      method: "POST",
      body: JSON.stringify({ titleId }),
    }),
  connectSeerr: (profileId: string, baseUrl: string, apiKey: string) =>
    request(`/profiles/${profileId}/sources/seerr/connect`, connectedSchema, {
      method: "POST",
      body: JSON.stringify({ baseUrl, apiKey }),
    }),
  listCommitments: (profileId: string) =>
    request(`/profiles/${profileId}/commitments`, commitmentListSchema),
  listMemory: (profileId: string) => request(`/profiles/${profileId}/memory`, memoryNoteListSchema),
  addMemoryNote: (profileId: string, text: string, kind: string) =>
    request(`/profiles/${profileId}/memory`, memoryNoteSchema, {
      method: "POST",
      body: JSON.stringify({ text, kind }),
    }),
  deleteMemoryNote: (profileId: string, noteId: string) =>
    request(`/profiles/${profileId}/memory/${noteId}`, z.null(), { method: "DELETE" }),
};
