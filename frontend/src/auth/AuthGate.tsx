import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Brand } from "../components/Brand";
import styles from "../components/components.module.css";
import { usePlexSignIn } from "./usePlexSignIn";

interface AuthGateProps {
  /** First-run setup: no account exists yet, so the form creates the owner account. */
  needsSetup: boolean;
  /** When true (and not in setup), local self-registration is offered alongside login. */
  registrationOpen: boolean;
  onLogin: (email: string, password: string) => void;
  onRegister: (email: string, password: string, displayName: string) => void;
  onPlexToken: (token: string) => void;
  error: string | null;
}

/** The unauthenticated entry screen. One component covers three modes: first-run owner setup,
 * normal login (+ optional open registration), and "Sign in with Plex" in every mode. */
export function AuthGate({
  needsSetup,
  registrationOpen,
  onLogin,
  onRegister,
  onPlexToken,
  error,
}: AuthGateProps): React.JSX.Element {
  const { t } = useTranslation("auth");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [displayName, setDisplayName] = useState("");
  // In setup we always register; otherwise the user toggles into a "create account" form.
  const [registerMode, setRegisterMode] = useState(false);
  const plex = usePlexSignIn(onPlexToken);

  const showRegister = needsSetup || registerMode;
  const canSubmit =
    email.trim() !== "" && password !== "" && (!showRegister || displayName.trim() !== "");

  function submit() {
    if (!canSubmit) {
      return;
    }
    if (showRegister) {
      onRegister(email.trim(), password, displayName.trim());
    } else {
      onLogin(email.trim(), password);
    }
  }

  const plexError =
    plex.status === "expired"
      ? t("plexExpired")
      : plex.status === "timeout"
        ? t("plexTimeout")
        : plex.status === "denied"
          ? (plex.error ?? t("plexFailed"))
          : plex.status === "error"
            ? t("plexFailed")
            : null;

  return (
    <main className={styles.gate} data-testid={needsSetup ? "setup-gate" : "login-gate"}>
      <Brand size={1.4} />
      <p className="muted">{needsSetup ? t("setupPrompt") : t("loginPrompt")}</p>
      <div className={styles.authForm}>
        {showRegister && (
          <>
            <label htmlFor="auth-display-name" className="sr-only">
              {t("displayName")}
            </label>
            <input
              id="auth-display-name"
              name="name"
              type="text"
              className="field"
              data-testid="auth-display-name"
              placeholder={t("displayName")}
              autoComplete="name"
              value={displayName}
              onChange={(e) => setDisplayName(e.target.value)}
            />
          </>
        )}
        <label htmlFor="auth-email" className="sr-only">
          {t("email")}
        </label>
        <input
          id="auth-email"
          name="email"
          type="email"
          className="field"
          data-testid="login-email"
          placeholder={t("email")}
          autoComplete="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
        />
        <label htmlFor="auth-password" className="sr-only">
          {t("password")}
        </label>
        <input
          id="auth-password"
          name="password"
          type="password"
          className="field"
          data-testid="login-password"
          placeholder={t("password")}
          autoComplete={showRegister ? "new-password" : "current-password"}
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && submit()}
        />
        <button
          type="button"
          className="btn btn-primary"
          data-testid={showRegister ? "auth-register-submit" : "login-submit"}
          onClick={submit}
          disabled={!canSubmit}
        >
          {showRegister ? (needsSetup ? t("createOwner") : t("createAccount")) : t("logIn")}
        </button>
        {!needsSetup && registrationOpen && (
          <button
            type="button"
            className={styles.authToggle}
            data-testid="auth-toggle-register"
            onClick={() => setRegisterMode((v) => !v)}
          >
            {registerMode ? t("haveAccount") : t("needAccount")}
          </button>
        )}
      </div>

      <div className={styles.authDivider}>{t("or")}</div>

      <button
        type="button"
        className="btn"
        data-testid="auth-plex-button"
        onClick={plex.start}
        disabled={plex.pending}
      >
        {plex.pending ? t("plexWaiting") : t("signInWithPlex")}
      </button>
      {needsSetup && <p className={styles.authHint}>{t("plexOwnerHint")}</p>}

      {(error !== null || plexError !== null) && (
        <p className={styles.error} data-testid="login-error">
          {error ?? plexError}
        </p>
      )}
    </main>
  );
}
