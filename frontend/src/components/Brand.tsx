import styles from "./components.module.css";

/** Placeholder lighthouse mark + wordmark. Identity is intentionally provisional (PR-final). */
export function Brand({ size = 1.15 }: { size?: number }): React.JSX.Element {
  return (
    <span className={styles.brand} style={{ fontSize: `${size}rem` }}>
      <span className={styles.beacon} aria-hidden="true">
        <BeaconGlyph />
      </span>
      Phare
    </span>
  );
}

export function BeaconGlyph(): React.JSX.Element {
  return (
    <svg
      width="1em"
      height="1em"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
      role="img"
      aria-label="lighthouse"
    >
      <path d="M12 2.5v2M5.5 7.5 4 6.8M18.5 7.5 20 6.8" />
      <circle cx="12" cy="8" r="2.4" />
      <path d="M9.4 10.2 8 21h8l-1.4-10.8" />
      <path d="M8.6 16h6.8" />
    </svg>
  );
}
