import { useState } from "react";
import { useTranslation } from "react-i18next";
import { translateTasteTerm } from "../lib/tasteVocab";
import styles from "../routes/routes.module.css";

/** A labelled set of taste chips the user can edit. Removing/adding writes the new list up to the
 * caller, which persists it as a taste override (overrides survive auto-regeneration).
 *
 * Items are the canonical stored keys (they drive affinity matching + overrides). Translation is
 * *display-only* — the stored value never changes, remove and dedupe still key on the canonical
 * value, so overrides survive a language switch (review F1). Display resolution order: the API's
 * `displayTerms` map (server-side LLM translation of free-form chips, passed via `display`), then
 * the static closed-vocab table, then the canonical value itself (offline / untranslated). */
export function EditableChips({
  label,
  items,
  tone,
  busy,
  display,
  onAdd,
  onRemove,
}: {
  label: string;
  items: string[];
  tone: "like" | "avoid";
  busy: boolean;
  // `| undefined` so callers under exactOptionalPropertyTypes can pass a possibly-absent map.
  display?: Record<string, string> | undefined;
  onAdd: (value: string) => void;
  onRemove: (value: string) => void;
}): React.JSX.Element {
  const { t, i18n } = useTranslation("profile");
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
        {items.map((g) => {
          // Translate for display only; the stored value `g` stays the canonical key. The server
          // map covers free-form chips, the vocab table covers closed-vocabulary terms — never both
          // (the backend excludes closed-vocab terms from its map), so no double translation.
          const shown = display?.[g] ?? translateTasteTerm(g, i18n.language);
          return (
            <span key={g} className={`chip ${chipClass}`} data-testid="taste-chip">
              {shown}
              <button
                type="button"
                className={styles.chipRemove}
                aria-label={t("chips.remove", { value: shown })}
                onClick={() => onRemove(g)}
                disabled={busy}
              >
                ×
              </button>
            </span>
          );
        })}
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
