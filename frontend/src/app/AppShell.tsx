import { Outlet } from "react-router-dom";
import { Brand } from "../components/Brand";
import { TabBar } from "../nav/TabBar";
import { ProfileProvider } from "./ProfileContext";
import styles from "./shell.module.css";

/** The tabbed shell: header, primary nav, and the active route. Wraps routes in the profile
 * context so each screen reads the active profile without prop-drilling. */
export function AppShell({ profileId }: { profileId: string }): React.JSX.Element {
  return (
    <ProfileProvider value={profileId}>
      <header className={styles.header}>
        <Brand />
      </header>
      <TabBar />
      <Outlet />
    </ProfileProvider>
  );
}
