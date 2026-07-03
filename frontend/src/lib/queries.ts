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
  onboarding: (id: string) => ["onboarding", id] as const,
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
    // 3-char minimum (mirrors Search.tsx): a 2-char substring match returns junk.
    enabled: q.length >= 3,
    staleTime: 60_000,
  });
}

/** Which sources the operator configured on this server (review D1). Server-wide + stable within a
 * session, so it's cached hard and not profile-scoped. */
export function useSourceCapabilities() {
  return useQuery({
    queryKey: ["sourceCapabilities"],
    queryFn: () => api.sourceCapabilities(),
    staleTime: Number.POSITIVE_INFINITY,
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

/** Send a card feedback signal ("not interested"). Deliberately does NOT invalidate the
 * recommendation queries: the card removes itself locally and keeps an undo affordance in its slot,
 * so a full refetch (which would yank that affordance) is left for the next natural navigation. The
 * negative signal does change history + taste, so those refresh. */
export function useSendTitleFeedback(profileId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: ({ titleId, signal }: { titleId: string; signal: "not_interested" }) =>
      api.sendTitleFeedback(profileId, titleId, signal),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: keys.history(profileId) });
      qc.invalidateQueries({ queryKey: keys.taste(profileId) });
    },
  });
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

/** Poll onboarding readiness while the sample seed runs, so ColdStart can show live step progress.
 * Stops polling once the profile is ready to browse (the app then reveals the tabbed shell). */
export function useOnboardingStatus(profileId: string, enabled: boolean) {
  return useQuery({
    queryKey: keys.onboarding(profileId),
    queryFn: () => api.onboardingStatus(profileId),
    enabled,
    refetchInterval: (query) => (query.state.data?.readyToBrowse ? false : 600),
  });
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

/** Trigger an on-demand taste generation (the empty-state "Generate now" button). A fresh taste
 * profile changes what we recommend, so refresh the taste + recommendation queries on success. */
export function useGenerateTaste(profileId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.generateTaste(profileId),
    onSuccess: () => {
      qc.invalidateQueries({ queryKey: keys.taste(profileId) });
      qc.invalidateQueries({ queryKey: keys.recommendations(profileId) });
      qc.invalidateQueries({ queryKey: keys.dynamic(profileId) });
    },
  });
}

export function useSyncTrakt(profileId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: (accessToken?: string) => api.syncTrakt(profileId, accessToken),
    onSuccess: () => invalidateProfileData(qc, profileId),
  });
}
