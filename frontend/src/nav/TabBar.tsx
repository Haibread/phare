import { NavLink } from "react-router-dom";
import styles from "./TabBar.module.css";

const ICONS: Record<string, React.JSX.Element> = {
  browse: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
      <rect x="3" y="4" width="18" height="16" rx="2" />
      <path d="M3 9h18M8 4v5M16 4v5" />
    </svg>
  ),
  chat: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
      <path d="M21 11.5a8.5 8.5 0 0 1-12.3 7.6L3 21l1.9-5.7A8.5 8.5 0 1 1 21 11.5Z" />
    </svg>
  ),
  profile: (
    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" aria-hidden="true">
      <circle cx="12" cy="8" r="4" />
      <path d="M4 21a8 8 0 0 1 16 0" />
    </svg>
  ),
};

const TABS = [
  { to: "/", key: "browse", label: "Browse" },
  { to: "/chat", key: "chat", label: "Chat" },
  { to: "/profile", key: "profile", label: "Profile" },
] as const;

export function TabBar(): React.JSX.Element {
  return (
    <nav className={styles.bar} aria-label="Primary">
      {TABS.map((tab) => (
        <NavLink
          key={tab.key}
          to={tab.to}
          end={tab.to === "/"}
          className={() => styles.tab}
          data-testid={`tab-${tab.key}`}
        >
          {ICONS[tab.key]}
          {tab.label}
        </NavLink>
      ))}
    </nav>
  );
}
