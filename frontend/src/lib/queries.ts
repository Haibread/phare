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
};

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

export function useGenerateTaste(profileId: string) {
  const qc = useQueryClient();
  return useMutation({
    mutationFn: () => api.generateTaste(profileId),
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
