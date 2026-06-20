import { useState } from "react";
import { Brand } from "../components/Brand";
import styles from "../components/components.module.css";

export function LoginGate({
  onLogin,
  error,
}: {
  onLogin: (password: string) => void;
  error: string | null;
}): React.JSX.Element {
  const [password, setPassword] = useState("");
  return (
    <main className={styles.gate} data-testid="login-gate">
      <Brand size={1.4} />
      <p className="muted">This instance is protected. Enter the password to continue.</p>
      <div className={styles.gateRow}>
        <input
          type="password"
          className="field"
          data-testid="login-password"
          aria-label="Password"
          placeholder="Password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && onLogin(password)}
        />
        <button
          type="button"
          className="btn btn-primary"
          data-testid="login-submit"
          onClick={() => onLogin(password)}
          disabled={password === ""}
        >
          Log in
        </button>
      </div>
      {error !== null && (
        <p className={styles.error} data-testid="login-error">
          {error}
        </p>
      )}
    </main>
  );
}
