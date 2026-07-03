import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { api, setAuthToken } from "../api";
import { errorMessage } from "../components/states";
import { keys } from "../lib/queries";
import styles from "./routes.module.css";

/** Account controls: sign out (revokes the session server-side) and change password (review I5). */
export function AccountCard(): React.JSX.Element {
  const { t } = useTranslation("profile");
  const { t: tCommon } = useTranslation("common");
  const qc = useQueryClient();
  const [current, setCurrent] = useState("");
  const [next, setNext] = useState("");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<{ tone: "ok" | "error"; text: string } | null>(null);

  async function signOut() {
    // Revoke server-side, then drop the local token either way so the app returns to the gate.
    try {
      await api.logout();
    } catch {
      // Even if the revoke call fails (offline), clear locally so this device is signed out.
    }
    setAuthToken(null);
    qc.invalidateQueries({ queryKey: keys.me });
  }

  async function submitPassword() {
    if (current === "" || next === "") return;
    setBusy(true);
    setNotice(null);
    try {
      const res = await api.changePassword(current, next);
      // The change revokes other sessions; the fresh token keeps this device logged in.
      setAuthToken(res.token);
      setCurrent("");
      setNext("");
      setNotice({ tone: "ok", text: t("account.passwordChanged") });
    } catch (e) {
      setNotice({ tone: "error", text: errorMessage(e, tCommon) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className={styles.card} data-testid="account-card">
      <div className={styles.cardHead}>
        <h2 style={{ fontSize: "1.05rem" }}>{t("account.heading")}</h2>
        <button type="button" className="btn" data-testid="logout" onClick={() => void signOut()}>
          {t("account.signOut")}
        </button>
      </div>
      <div className={styles.form} style={{ marginTop: "var(--sp-2)" }}>
        <label htmlFor="account-current" className="sr-only">
          {t("account.currentPassword")}
        </label>
        <input
          id="account-current"
          name="current-password"
          type="password"
          className="field"
          data-testid="current-password"
          placeholder={t("account.currentPassword")}
          autoComplete="current-password"
          value={current}
          onChange={(e) => setCurrent(e.target.value)}
        />
        <label htmlFor="account-new" className="sr-only">
          {t("account.newPassword")}
        </label>
        <input
          id="account-new"
          name="new-password"
          type="password"
          className="field"
          data-testid="new-password"
          placeholder={t("account.newPassword")}
          autoComplete="new-password"
          value={next}
          onChange={(e) => setNext(e.target.value)}
        />
        <button
          type="button"
          className="btn btn-primary"
          data-testid="change-password"
          disabled={busy || current === "" || next === ""}
          onClick={() => void submitPassword()}
        >
          {t("account.changePassword")}
        </button>
        {notice && (
          <p
            className={notice.tone === "error" ? styles.errorText : "muted"}
            data-testid="account-notice"
            role={notice.tone === "error" ? "alert" : undefined}
          >
            {notice.text}
          </p>
        )}
      </div>
    </section>
  );
}
