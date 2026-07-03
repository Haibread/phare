import { useQueryClient } from "@tanstack/react-query";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { BeaconGlyph } from "../components/Brand";
import { ErrorState } from "../components/states";
import { keys, useLoadSampleData, useOnboardingStatus, useSeedCatalog } from "../lib/queries";
import { SourcePicker } from "./SourcePicker";
import styles from "./onboarding.module.css";

type StepState = "done" | "active" | "pending";

/** First-run takeover: one connect call-to-action + a sample-data escape hatch. Shown until the
 * profile has any history, at which point the app reveals the tabbed shell. */
export function ColdStart({ profileId }: { profileId: string }): React.JSX.Element {
  const { t } = useTranslation("onboarding");
  const qc = useQueryClient();
  const [pickerOpen, setPickerOpen] = useState(false);
  const [seeding, setSeeding] = useState(false);
  const sample = useLoadSampleData(profileId);
  const catalog = useSeedCatalog();
  // Poll real backend readiness while the seed runs, so the steps reflect actual progress.
  const status = useOnboardingStatus(profileId, seeding);

  function invalidate() {
    qc.invalidateQueries({ queryKey: keys.history(profileId) });
    qc.invalidateQueries({ queryKey: keys.taste(profileId) });
    qc.invalidateQueries({ queryKey: keys.recommendations(profileId) });
    qc.invalidateQueries({ queryKey: keys.dynamic(profileId) });
  }

  async function exploreSample() {
    setSeeding(true);
    await catalog.mutateAsync();
    // Landing is gated on history existing (App), and taste finishes in the background — so the
    // user drops into Browse (with its "building your profile" state) as soon as this resolves.
    await sample.mutateAsync();
  }

  if (seeding && !sample.isError) {
    // The three steps are driven by the polled onboarding status; the first not-yet-done one is
    // "active". Catalog is seeded before history, so they light up in order.
    const done = [
      status.data?.catalogReady ?? false,
      status.data?.historyReady ?? false,
      status.data?.tasteReady ?? false,
    ];
    const activeIndex = done.indexOf(false);
    const steps: Array<{ key: string; state: StepState }> = ["catalog", "history", "taste"].map(
      (key, i) => ({
        key,
        state: done[i] ? "done" : i === activeIndex ? "active" : "pending",
      }),
    );
    return (
      <main className={styles.cold} data-testid="cold-start">
        <span className={styles.halo} aria-hidden="true">
          <BeaconGlyph />
        </span>
        <h1 className={styles.coldTitle}>{t("coldStart.steps.heading")}</h1>
        <ol className={styles.steps} data-testid="onboarding-steps">
          {steps.map((step) => (
            <li key={step.key} className={styles.step} data-state={step.state}>
              <span className={styles.stepMark} aria-hidden="true">
                {step.state === "done" ? "✓" : step.state === "active" ? "•" : ""}
              </span>
              <span>{t(`coldStart.steps.${step.key}`)}</span>
            </li>
          ))}
        </ol>
      </main>
    );
  }

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
      >
        {t("coldStart.exploreSample")}
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
