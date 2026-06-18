import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useState } from "react";
import { Route, Routes } from "react-router-dom";
import { api, setAuthToken } from "../api";
import { LoginGate } from "../auth/LoginGate";
import { Loading } from "../components/states";
import { keys, useCreateProfile, useHistory, useMe, useProfiles } from "../lib/queries";
import { ColdStart } from "../onboarding/ColdStart";
import { Browse } from "../routes/Browse";
import { Chat } from "../routes/Chat";
import { Profile } from "../routes/Profile";
import { Search } from "../routes/Search";
import { AppShell } from "./AppShell";

export function App(): React.JSX.Element {
  const qc = useQueryClient();
  const me = useMe();
  const [loginError, setLoginError] = useState<string | null>(null);

  function onLogin(password: string) {
    setLoginError(null);
    api
      .login(password)
      .then((r) => {
        setAuthToken(r.token);
        return qc.invalidateQueries({ queryKey: keys.me });
      })
      .catch((e: unknown) => setLoginError(e instanceof Error ? e.message : String(e)));
  }

  if (me.isPending) {
    return <Loading />;
  }
  if (me.data?.authRequired && !me.data.authenticated) {
    return <LoginGate onLogin={onLogin} error={loginError} />;
  }

  return <Bootstrapped />;
}

/** Resolves the single active profile (creating a default one on first run), then routes to the
 * onboarding gate or the tabbed shell depending on whether there's any history yet. */
function Bootstrapped(): React.JSX.Element {
  const profiles = useProfiles();
  const createProfile = useCreateProfile();

  useEffect(() => {
    if (
      profiles.data &&
      profiles.data.items.length === 0 &&
      createProfile.isIdle &&
      !createProfile.isPending
    ) {
      createProfile.mutate("Me");
    }
  }, [profiles.data, createProfile]);

  const profileId = profiles.data?.items[0]?.id ?? null;
  const history = useHistory(profileId);

  if (profileId === null || history.isPending) {
    return <Loading />;
  }

  // First run: no events yet → onboarding takeover (no tabs).
  if ((history.data?.items.length ?? 0) === 0) {
    return <ColdStart profileId={profileId} />;
  }

  return (
    <Routes>
      <Route element={<AppShell profileId={profileId} />}>
        <Route index element={<Browse />} />
        <Route path="search" element={<Search />} />
        <Route path="chat" element={<Chat />} />
        <Route path="profile" element={<Profile />} />
      </Route>
    </Routes>
  );
}
