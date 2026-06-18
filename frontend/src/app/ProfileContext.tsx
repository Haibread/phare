import { createContext, useContext } from "react";

/** The active profile id, resolved once at bootstrap and provided to every route. One account =
 * one user (principle 7), so the rest of the app never juggles profile selection. */
const ProfileContext = createContext<string | null>(null);

export const ProfileProvider = ProfileContext.Provider;

export function useProfileId(): string {
  const id = useContext(ProfileContext);
  if (id === null) {
    throw new Error("useProfileId must be used within a ProfileProvider");
  }
  return id;
}
