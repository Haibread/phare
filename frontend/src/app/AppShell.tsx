import { NavLink, Outlet } from "react-router-dom";
import { Brand } from "../components/Brand";
import { TabBar } from "../nav/TabBar";
import { ChatProvider } from "./ChatContext";
import { ProfileProvider } from "./ProfileContext";
import styles from "./shell.module.css";

/** The tabbed shell: header, primary nav, and the active route. Wraps routes in the profile
 * context so each screen reads the active profile without prop-drilling. */
export function AppShell({ profileId }: { profileId: string }): React.JSX.Element {
  return (
    <ProfileProvider value={profileId}>
      <ChatProvider>
        <header className={styles.header}>
          <Brand />
          <NavLink
            to="/search"
            className={() => styles.searchLink}
            aria-label="Search"
            data-testid="open-search"
          >
            <svg
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth="2"
              aria-hidden="true"
            >
              <circle cx="11" cy="11" r="7" />
              <path d="m20 20-3.5-3.5" />
            </svg>
          </NavLink>
        </header>
        <TabBar />
        <Outlet />
      </ChatProvider>
    </ProfileProvider>
  );
}
