import { useTranslation } from "react-i18next";
// Official TMDB "primary short" wordmark, committed verbatim from themoviedb.org's branding page
// (the gradient reads on our dark background). Rendered unmodified — no recolor/distortion — per
// TMDB's branding guidelines (review J2).
import tmdbLogo from "../assets/tmdb-logo.svg";
import styles from "./routes.module.css";

/** Legal attributions (review J2). TMDB's terms require the exact sentence below, a link, *and*
 * their logo; the sentence stays verbatim (not translated). The surrounding heading + the logo's
 * alt text are localized. */
export function Attributions(): React.JSX.Element {
  const { t } = useTranslation("profile");
  return (
    <footer className={styles.attributions} data-testid="attributions">
      <h2 style={{ fontSize: "0.95rem" }}>{t("about.heading")}</h2>
      <a
        href="https://www.themoviedb.org/"
        target="_blank"
        rel="noopener noreferrer"
        className={styles.tmdbLogoLink}
      >
        <img src={tmdbLogo} alt={t("about.tmdbLogoAlt")} className={styles.tmdbLogo} />
      </a>
      <p>
        This product uses the TMDB API but is not endorsed or certified by{" "}
        <a href="https://www.themoviedb.org/" target="_blank" rel="noopener noreferrer">
          TMDB
        </a>
        .
      </p>
      <p className="faint" style={{ fontSize: "0.8rem" }}>
        {t("about.sources")}
      </p>
    </footer>
  );
}
