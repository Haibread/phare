import { useTranslation } from "react-i18next";
import { LANGUAGE_LABELS, type Language, SUPPORTED_LANGUAGES } from "../i18n";
import styles from "./LanguageSelector.module.css";

/** Header language switcher. Persists the choice via i18next's localStorage detector cache, which
 * in turn drives Accept-Language on the API client (see src/i18n.ts). */
export function LanguageSelector(): React.JSX.Element {
  const { i18n, t } = useTranslation("common");
  const current: Language =
    SUPPORTED_LANGUAGES.find((lng) => lng === i18n.resolvedLanguage) ?? "en";
  return (
    <select
      className={styles.select}
      value={current}
      onChange={(event) => void i18n.changeLanguage(event.target.value)}
      aria-label={t("language.label")}
      data-testid="language-selector"
    >
      {SUPPORTED_LANGUAGES.map((lng) => (
        <option key={lng} value={lng}>
          {LANGUAGE_LABELS[lng]}
        </option>
      ))}
    </select>
  );
}
