import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { Route, Routes } from "react-router-dom";
import { api, setAuthToken } from "../api";
import { AuthGate } from "../auth/AuthGate";
import { ErrorState, Loading } from "../components/states";
import { keys, useHistory, useMe, useProfiles } from "../lib/queries";
import { ColdStart } from "../onboarding/ColdStart";
import { Browse } from "../routes/Browse";
import { Chat } from "../routes/Chat";
import { Profile } from "../routes/Profile";
import { Search } from "../routes/Search";
import { AppShell } from "./AppShell";

export function App(): React.JSX.Element {
  const qc = useQueryClient();
  const me = useMe();
  const [authError, setAuthError] = useState<string | null>(null);

  function applyToken(token: string) {
    setAuthError(null);
    setAuthToken(token);
    return qc.invalidateQueries({ queryKey: keys.me });
  }

  function reportError(e: unknown) {
    setAuthError(e instanceof Error ? e.message : String(e));
  }

  function onLogin(email: string, password: string) {
    setAuthError(null);
    api
      .login(email, password)
      .then((r) => applyToken(r.token))
      .catch(reportError);
  }

  function onRegister(email: string, password: string, displayName: string) {
    setAuthError(null);
    api
      .register(email, password, displayName)
      .then((r) => {
        // `token` is non-null only when self-registering (first-run / open registration).
        if (r.token !== null) {
          return applyToken(r.token);
        }
        return qc.invalidateQueries({ queryKey: keys.me });
      })
      .catch(reportError);
  }

  function onPlexToken(token: string) {
    applyToken(token);
  }

  if (me.isPending) {
    return <Loading />;
  }
  if (me.data?.needsSetup || me.data?.authenticated !== true) {
    return (
      <AuthGate
        needsSetup={me.data?.needsSetup ?? false}
        registrationOpen={me.data?.registrationOpen ?? false}
        onLogin={onLogin}
        onRegister={onRegister}
        onPlexToken={onPlexToken}
        error={authError}
      />
    );
  }

  return <Bootstrapped profileId={me.data.user?.profileId ?? null} />;
}

/** Routes to the onboarding gate or the tabbed shell depending on whether there's any history yet.
 * The active profile id comes from the authenticated user; if it's somehow missing we fall back to
 * the caller's single profile from `GET /profiles`. */
function Bootstrapped({ profileId }: { profileId: string | null }): React.JSX.Element {
  const profiles = useProfiles();
  const resolvedProfileId = profileId ?? profiles.data?.items[0]?.id ?? null;
  const history = useHistory(resolvedProfileId);

  if (resolvedProfileId === null || history.isPending) {
    return <Loading />;
  }

  // A backend hiccup must NOT masquerade as "no history" — that would bounce an existing user
  // back into onboarding. Only a *successful* empty history is first-run.
  if (history.isError) {
    return <ErrorState error={history.error} onRetry={() => history.refetch()} />;
  }

  // First run: no events yet → onboarding takeover (no tabs).
  if ((history.data?.items.length ?? 0) === 0) {
    return <ColdStart profileId={resolvedProfileId} />;
  }

  return (
    <Routes>
      <Route element={<AppShell profileId={resolvedProfileId} />}>
        <Route index element={<Browse />} />
        <Route path="search" element={<Search />} />
        <Route path="chat" element={<Chat />} />
        <Route path="profile" element={<Profile />} />
      </Route>
    </Routes>
  );
}
