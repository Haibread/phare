import { createContext, useContext } from "react";
import { useTranslation } from "react-i18next";
import styles from "./components.module.css";

/** Library availability + a request handler, provided by Browse to every poster card. Null when
 * there's no provider in the tree (e.g. unit tests, or screens that don't fetch availability). */
export interface AvailabilityCtx {
  configured: boolean;
  results: Record<string, string>;
  requestingId: string | null;
  onRequest: (titleId: string) => void;
}

const Ctx = createContext<AvailabilityCtx | null>(null);
export const AvailabilityProvider = Ctx.Provider;

export function TitleAction({ titleId }: { titleId: string }): React.JSX.Element | null {
  const { t } = useTranslation("title");
  const ctx = useContext(Ctx);
  if (ctx === null || !ctx.configured) {
    return null;
  }
  const status = ctx.results[titleId];
  if (status === undefined || status === "unknown") {
    return null;
  }
  if (status === "available") {
    return (
      <span className={`${styles.availTag} ${styles.availOk}`} data-testid="title-available">
        {t("availability.available")}
      </span>
    );
  }
  if (status === "queued") {
    return (
      <span className={`${styles.availTag} ${styles.availQueued}`} data-testid="title-queued">
        {t("availability.queued")}
      </span>
    );
  }
  return (
    <button
      type="button"
      className={styles.requestBtn}
      data-testid="title-request"
      onClick={() => ctx.onRequest(titleId)}
      disabled={ctx.requestingId === titleId}
    >
      {ctx.requestingId === titleId ? t("availability.requesting") : t("availability.request")}
    </button>
  );
}
