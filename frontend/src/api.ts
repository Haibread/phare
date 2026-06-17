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
  components: z.record(z.number()),
});
export type RecommendationItem = z.infer<typeof recommendationItemSchema>;

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

export const chatReplySchema = z.object({
  replyText: z.string(),
  intent: chatIntentSchema,
  items: z.array(recommendationItemSchema),
});
export type ChatReply = z.infer<typeof chatReplySchema>;

const catalogSummarySchema = z.object({ created: z.number() });

const meSchema = z.object({ authRequired: z.boolean(), authenticated: z.boolean() });
export type Me = z.infer<typeof meSchema>;
const tokenSchema = z.object({ token: z.string() });

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
  return schema.parse(await response.json());
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
  syncTrakt: (profileId: string, accessToken: string) =>
    request("/sources/trakt/sync", ingestSummarySchema, {
      method: "POST",
      body: JSON.stringify({ profileId, accessToken }),
    }),
  getTaste: (profileId: string) => request(`/profiles/${profileId}/taste`, tasteSchema),
  generateTaste: (profileId: string) =>
    request(`/profiles/${profileId}/taste/generate`, tasteSchema, { method: "POST" }),
  seedCatalog: () => request("/catalog/sample", catalogSummarySchema, { method: "POST" }),
  recommendations: (profileId: string) =>
    request(`/profiles/${profileId}/recommendations`, recommendationsResponseSchema),
  chat: (profileId: string, message: string) =>
    request(`/profiles/${profileId}/chat`, chatReplySchema, {
      method: "POST",
      body: JSON.stringify({ message }),
    }),
};
