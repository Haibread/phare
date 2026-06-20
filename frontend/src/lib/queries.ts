/** react-query hooks over the typed API client. Query keys are centralised so mutations can
 * invalidate precisely (e.g. loading sample data refreshes history + recommendations). */

import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { api } from "../api";

export const keys = {
  me: ["me"] as const,
  profiles: ["profiles"] as const,
  history: (id: string) => ["history", id] as const,
  taste: (id: string) => ["taste", id] as const,
  recommendations: (id: string) => ["recommendations", id] as const,
  dynamic: (id: string) => ["dynamic", id] as const,
  conversion: (id: string) => ["conversion", id] as const,
  availability: (id: string) => ["availability", id] as const,
  sources: (id: string) => ["sources", id] as const,
  commitments: (id: string) => ["commitments", id] as const,
  memory: (id: string) => ["memory", id] as const,
  titleDetail: (id: string) => ["titleDetail", id] as const,
  titleExplanation: (profileId: string, titleId: string) =>
    ["titleExplanation", profileId, titleId] as const,
};

/** Lazily fetch a title's "more info" (synopsis/runtime/links); only runs while the sheet is open. */
export function useTitleDetail(titleId: string, enabled: boolean) {
  return useQuery({
    queryKey: keys.titleDetail(titleId),
    queryFn: () => api.titleDetail(titleId),
    enabled,
    staleTime: 5 * 60_000, // title metadata is stable; don't refetch on every open
  });
}

/** Lazily generate the LLM "why this fits you" reason — only when the detail sheet is open, so we
 * don't pay to explain cards nobody opens. Cached, so re-opening is free. */
export function useTitleExplanation(profileId: string, titleId: string, enabled: boolean) {
  return useQuery({
    queryKey: keys.titleExplanation(profileId, titleId),
    queryFn: () => api.titleExplanation(profileId, titleId),
    enabled,
    staleTime: 10 * 60_000,
  });
}

export function useMe() {
  return useQuery({ queryKey: keys.me, queryFn: api.me });
}

export function useProfiles() {
  return useQuery({ queryKey: keys.profiles, queryFn: api.listProfiles });
}

export function useHistory(profileId: string | null) {
  return useQuery({
    queryKey: keys.history(profileId ?? ""),
    queryFn: () => api.history(profileId as string),
    enabled: profileId !== null,
  });
}

export function useTaste(profileId: string | null) {
  return useQuery({
    queryKey: keys.taste(profileId ?? ""),
    queryFn: () => api.getTaste(profileId as string),
    enabled: profileId !== null,
    // A profile with no taste yet returns 404; treat that as "no data", not an error to retry.
    retry: false,
  });
}

export function useRecommendations(profileId: string | null) {
  return useQuery({
    queryKey: keys.recommendations(profileId ?? ""),
    queryFn: () => api.recommendations(profileId as string),
    enabled: profileId !== null,
  });
}

export function useDynamicRows(profileId: string | null) {
  return useQuery({
    queryKey: keys.dynamic(profileId ?? ""),
    queryFn: () => api.dynamicRecommendations(profileId as string),
    enabled: profileId !== null,
  });
}

export function useSearch(profileId: string, query: string) {
  const q = query.trim();
  return useQuery({
    queryKey: ["search", profileId, q],
    queryFn: () => api.searchCatalog(profileId, q),
    enabled: q.length >= 2,
    staleTime: 60_000,
  });
}

export function useConnectedSources(profileId: string | null) {
  return useQuery({
    queryKey: keys.sources(profileId ?? ""),
    queryFn: () => api.listSources(profileId as string),
    enabled: profileId !== null,
  });
}

export function useConversion(profileId: string | null) {
  return useQuery({
    queryKey: keys.conversion(profileId ?? ""),
    queryFn: () => api.conversion(profileId as string),
    enabled: profileId !== null,
  });
}

export function useCreateProfile() {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (displayName: string) => api.createProfile(displayName),
    onSuccess: () => qc.invalidateQueries({ queryKey: keys.profiles }),
  });
}

/** Invalidate everything that depends on a profile's events. */
function invalidateProfileData(qc: ReturnType<typeof useQueryClient>, profileId: string) {
  qc.invalidateQueries({ queryKey: keys.history(profileId) });
  qc.invalidateQueries({ queryKey: keys.taste(profileId) });
  qc.invalidateQueries({ queryKey: keys.recommendations(profileId) });
  qc.invalidateQueries({ queryKey: keys.dynamic(profileId) });
}

/** A callback that refreshes everything a chat write can change (signals/taste/memory/plans), so
 * Browse and Profile reflect "I loved X" without a manual reload. */
export function useInvalidateAfterChat(profileId: string): () => void {
  const qc = useQueryClient();
  return () => {
    invalidateProfileData(qc, profileId);
    qc.invalidateQueries({ queryKey: keys.memory(profileId) });
    qc.invalidateQueries({ queryKey: keys.commitments(profileId) });
  };
}

export function useLoadSampleData(profileId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.loadSampleData(profileId),
    onSuccess: () => invalidateProfileData(qc, profileId),
  });
}

export function useSeedCatalog() {
  return useMutation({ mutationFn: () => api.seedCatalog() });
}

export function useAvailability(profileId: string, titleIds: string[]) {
  const idsKey = [...titleIds].sort().join(",");
  return useQuery({
    queryKey: [...keys.availability(profileId), idsKey],
    queryFn: () => api.availability(profileId, titleIds),
    enabled: titleIds.length > 0,
    staleTime: 60_000,
  });
}

export function useRequestTitle(profileId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (titleId: string) => api.requestTitle(profileId, titleId),
    onSuccess: () => qc.invalidateQueries({ queryKey: keys.availability(profileId) }),
  });
}

export function useChatOpening(profileId: string) {
  return useQuery({
    queryKey: ["chatOpening", profileId],
    queryFn: () => api.chatOpening(profileId),
    staleTime: 0,
  });
}

export function useUndoChatAction(profileId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (token: string) => api.undoChatAction(profileId, token),
    onSuccess: () => {
      // A reversed write changes signals/taste/memory — refresh anything derived from them.
      invalidateProfileData(qc, profileId);
      qc.invalidateQueries({ queryKey: keys.memory(profileId) });
      qc.invalidateQueries({ queryKey: keys.commitments(profileId) });
    },
  });
}

export function useCommitments(profileId: string | null) {
  return useQuery({
    queryKey: keys.commitments(profileId ?? ""),
    queryFn: () => api.listCommitments(profileId as string),
    enabled: profileId !== null,
  });
}

export function useMemory(profileId: string | null) {
  return useQuery({
    queryKey: keys.memory(profileId ?? ""),
    queryFn: () => api.listMemory(profileId as string),
    enabled: profileId !== null,
  });
}

export function useAddMemoryNote(profileId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ text, kind }: { text: string; kind: string }) =>
      api.addMemoryNote(profileId, text, kind),
    onSuccess: () => qc.invalidateQueries({ queryKey: keys.memory(profileId) }),
  });
}

export function useDeleteMemoryNote(profileId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (noteId: string) => api.deleteMemoryNote(profileId, noteId),
    onSuccess: () => qc.invalidateQueries({ queryKey: keys.memory(profileId) }),
  });
}

export function useUpdateTaste(profileId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (userOverrides: Record<string, unknown>) =>
      api.updateTaste(profileId, userOverrides),
    onSuccess: () => qc.invalidateQueries({ queryKey: keys.taste(profileId) }),
  });
}

export function useSyncTrakt(profileId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (accessToken?: string) => api.syncTrakt(profileId, accessToken),
    onSuccess: () => invalidateProfileData(qc, profileId),
  });
}
