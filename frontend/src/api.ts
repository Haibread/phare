/** Typed API client. All responses are validated with zod at the I/O boundary. */

import { z } from "zod";
import { logger } from "./logger";

const API_BASE = import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000";

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
});

const chatIntentSchema = z.object({
  maxRuntime: z.number().nullable(),
  includeGenres: z.array(z.string()),
  excludeGenres: z.array(z.string()),
  mood: z.string().nullable(),
});

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
});
export type ChatReply = z.infer<typeof chatReplySchema>;

// The first SSE event of a streamed turn: the picks + writes, before the reply text streams in.
export const chatStreamMetaSchema = z.object({
  intent: chatIntentSchema,
  items: z.array(recommendationItemSchema),
  actions: z.array(agentActionSchema),
});
export type ChatStreamMeta = z.infer<typeof chatStreamMetaSchema>;

export interface ChatStreamHandlers {
  onMeta?: (meta: ChatStreamMeta) => void;
  onDelta?: (text: string) => void;
  onDone?: () => void;
}

const chatOpeningSchema = z.object({ greeting: z.string().nullable() });
const undoResultSchema = z.object({ undone: z.boolean() });

const catalogSummarySchema = z.object({ created: z.number() });

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

const meSchema = z.object({ authRequired: z.boolean(), authenticated: z.boolean() });
export type Me = z.infer<typeof meSchema>;
const tokenSchema = z.object({ token: z.string() });

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

// Bearer token held in memory only (never localStorage) — secrets stay in the session.
let authToken: string | null = null;
export function setAuthToken(token: string | null): void {
  authToken = token;
}

async function request<T>(path: string, schema: z.ZodType<T>, init?: RequestInit): Promise<T> {
  const url = `${API_BASE}${path}`;
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (authToken !== null) {
    headers.Authorization = `Bearer ${authToken}`;
  }
  const response = await fetch(url, {
    ...init,
    headers: { ...headers, ...(init?.headers as Record<string, string> | undefined) },
  });
  if (!response.ok) {
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
    throw new Error(detail);
  }
  logger.debug("api.ok", { url, status: response.status });
  // 204 No Content (e.g. DELETE) has no body to parse — validate against null.
  if (response.status === 204) {
    return schema.parse(null);
  }
  return schema.parse(await response.json());
}

/** Stream a chat turn over Server-Sent Events: the picks/writes arrive in one `meta` event, then
 * the reply text streams in as `delta` events. Resolves when the stream closes (`done`). */
async function chatStream(
  profileId: string,
  message: string,
  handlers: ChatStreamHandlers,
  signal?: AbortSignal,
): Promise<void> {
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (authToken !== null) {
    headers.Authorization = `Bearer ${authToken}`;
  }
  const response = await fetch(`${API_BASE}/profiles/${profileId}/chat/stream`, {
    method: "POST",
    headers,
    body: JSON.stringify({ message }),
    signal: signal ?? null,
  });
  if (!response.ok || response.body === null) {
    throw new Error(response.statusText || "chat stream failed");
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
        const parsed: unknown = JSON.parse(data);
        if (event === "meta") {
          handlers.onMeta?.(chatStreamMetaSchema.parse(parsed));
        } else if (event === "delta") {
          handlers.onDelta?.(String((parsed as { text: string }).text));
        } else if (event === "done") {
          handlers.onDone?.();
        }
      }
      sep = buffer.indexOf("\n\n");
    }
  }
}

export const api = {
  me: () => request("/me", meSchema),
  login: (password: string) =>
    request("/auth/login", tokenSchema, {
      method: "POST",
      body: JSON.stringify({ password }),
    }),
  listProfiles: () => request("/profiles", profilePageSchema),
  createProfile: (displayName: string) =>
    request("/profiles", profileSchema, {
      method: "POST",
      body: JSON.stringify({ displayName }),
    }),
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
