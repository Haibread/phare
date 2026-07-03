import { useTranslation } from "react-i18next";
import styles from "./routes.module.css";

/** Legal attributions (review J2). TMDB's terms require the exact sentence below plus a link; it
 * stays verbatim (not translated). The surrounding heading is localized. The official TMDB logo
 * asset should be dropped in alongside this text to fully satisfy their branding guidelines. */
export function Attributions(): React.JSX.Element {
  const { t } = useTranslation("profile");
  return (
    <footer className={styles.attributions} data-testid="attributions">
      <h2 style={{ fontSize: "0.95rem" }}>{t("about.heading")}</h2>
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
