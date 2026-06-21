import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { BeaconGlyph } from "../components/Brand";
import { ErrorState } from "../components/states";
import { keys, useLoadSampleData, useSeedCatalog } from "../lib/queries";
import { SourcePicker } from "./SourcePicker";
import styles from "./onboarding.module.css";

/** First-run takeover: one connect call-to-action + a sample-data escape hatch. Shown until the
 * profile has any history, at which point the app reveals the tabbed shell. */
export function ColdStart({ profileId }: { profileId: string }): React.JSX.Element {
  const { t } = useTranslation("onboarding");
  const qc = useQueryClient();
  const [pickerOpen, setPickerOpen] = useState(false);
  const sample = useLoadSampleData(profileId);
  const catalog = useSeedCatalog();

  function invalidate() {
    qc.invalidateQueries({ queryKey: keys.history(profileId) });
    qc.invalidateQueries({ queryKey: keys.taste(profileId) });
    qc.invalidateQueries({ queryKey: keys.recommendations(profileId) });
    qc.invalidateQueries({ queryKey: keys.dynamic(profileId) });
  }

  async function exploreSample() {
    await catalog.mutateAsync();
    await sample.mutateAsync();
  }

  const busy = sample.isPending || catalog.isPending;

  return (
    <main className={styles.cold} data-testid="cold-start">
      <span className={styles.halo} aria-hidden="true">
        <BeaconGlyph />
      </span>
      <h1 className={styles.coldTitle}>{t("coldStart.title")}</h1>
      <p className={styles.lede}>{t("coldStart.lede")}</p>

      <button
        type="button"
        className={`btn btn-primary ${styles.cta}`}
        data-testid="open-source-picker"
        onClick={() => setPickerOpen(true)}
      >
        {t("coldStart.connectLibrary")}
      </button>

      <div className={styles.or}>{t("coldStart.or")}</div>

      <button
        type="button"
        className={styles.link}
        data-testid="explore-sample"
        onClick={() => void exploreSample()}
        disabled={busy}
      >
        {busy ? t("coldStart.loadingSample") : t("coldStart.exploreSample")}
      </button>
      <p className="faint" style={{ fontSize: "0.8rem", maxWidth: "16rem" }}>
        {t("coldStart.sampleHint")}
      </p>

      {sample.isError && <ErrorState error={sample.error} />}

      <SourcePicker
        profileId={profileId}
        open={pickerOpen}
        onOpenChange={setPickerOpen}
        onConnected={invalidate}
      />
    </main>
  );
}
