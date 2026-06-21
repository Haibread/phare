import { useState } from "react";
import { useTranslation } from "react-i18next";
import styles from "../routes/routes.module.css";

/** A labelled set of taste chips the user can edit. Removing/adding writes the new list up to the
 * caller, which persists it as a taste override (overrides survive auto-regeneration). */
export function EditableChips({
  label,
  items,
  tone,
  busy,
  onAdd,
  onRemove,
}: {
  label: string;
  items: string[];
  tone: "like" | "avoid";
  busy: boolean;
  onAdd: (value: string) => void;
  onRemove: (value: string) => void;
}): React.JSX.Element {
  const { t } = useTranslation("profile");
  const [draft, setDraft] = useState("");
  const chipClass = tone === "like" ? styles.chipLike : styles.chipAvoid;

  function add() {
    const value = draft.trim();
    if (value !== "" && !items.includes(value)) {
      onAdd(value);
    }
    setDraft("");
  }

  return (
    <>
      <div className="faint" style={{ fontSize: "0.75rem" }}>
        {label}
      </div>
      <div className={styles.chips} data-testid={`taste-${tone}`}>
        {items.map((g) => (
          <span key={g} className={`chip ${chipClass}`} data-testid="taste-chip">
            {g}
            <button
              type="button"
              className={styles.chipRemove}
              aria-label={t("chips.remove", { value: g })}
              onClick={() => onRemove(g)}
              disabled={busy}
            >
              ×
            </button>
          </span>
        ))}
        <input
          className={styles.chipInput}
          data-testid={`taste-add-${tone}`}
          placeholder={t("chips.add")}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && add()}
          onBlur={add}
          disabled={busy}
        />
      </div>
    </>
  );
}
