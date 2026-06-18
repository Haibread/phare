import styles from "./components.module.css";

/** The lighthouse mark + wordmark. */
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

/** A line-art lighthouse: light beams, lamp room + roof, a tapered banded tower, and a base. */
export function BeaconGlyph(): React.JSX.Element {
  return (
    <svg
      width="1em"
      height="1em"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      strokeWidth="1.7"
      strokeLinecap="round"
      strokeLinejoin="round"
      role="img"
      aria-label="lighthouse"
    >
      <path d="M9.6 6.1 6.7 4.8M14.4 6.1 17.3 4.8" />
      <path d="M9.8 6 12 3.7 14.2 6" />
      <path d="M10 6h4v2.4h-4z" />
      <path d="M9.2 8.4h5.6" />
      <path d="M9.7 8.6 8.7 20M14.3 8.6 15.3 20" />
      <path d="M9.1 14.3h5.8M7.6 20h8.8" />
    </svg>
  );
}
