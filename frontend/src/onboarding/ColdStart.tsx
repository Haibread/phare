import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { BeaconGlyph } from "../components/Brand";
import { ErrorState } from "../components/states";
import {
  keys,
  useLoadSampleData,
  useOnboardingStatus,
  useSeedCatalog,
  useSourceCapabilities,
} from "../lib/queries";
import { ScratchStart } from "./ScratchStart";
import { SourcePicker } from "./SourcePicker";
import styles from "./onboarding.module.css";

type StepState = "done" | "active" | "pending";

// How long the "N titles imported" confirmation lingers before it auto-advances (review D4).
const CONFIRM_DWELL_MS = 3000;

/** First-run takeover: one connect call-to-action + a sample-data escape hatch. Shown until the
 * profile has any history, at which point the app reveals the tabbed shell. */
export function ColdStart({ profileId }: { profileId: string }): React.JSX.Element {
  const { t } = useTranslation("onboarding");
  const qc = useQueryClient();
  const [pickerOpen, setPickerOpen] = useState(false);
  const [seeding, setSeeding] = useState(false);
  // "Start from scratch": the by-hand favorite-seeding path, for a user with no library to connect.
  const [scratch, setScratch] = useState(false);
  // Set to the imported count once a source sync finishes, so we show a brief confirmation instead
  // of hard-cutting to Browse the instant history exists (review D4). Landing is deferred until the
  // user (or the timer) continues, by holding back the history invalidation until then.
  const [synced, setSynced] = useState<number | null>(null);
  const sample = useLoadSampleData(profileId);
  const catalog = useSeedCatalog();
  // The sample path 403s in production; only offer the escape hatch when the server actually
  // supports it, so we don't hand the user a button that hangs on the rejection.
  const capabilities = useSourceCapabilities();
  const sampleAvailable = capabilities.data?.sampleData ?? false;
  // Poll real backend readiness while the seed runs, so the steps reflect actual progress.
  const status = useOnboardingStatus(profileId, seeding);

  const invalidate = useCallback(() => {
    qc.invalidateQueries({ queryKey: keys.history(profileId) });
    qc.invalidateQueries({ queryKey: keys.taste(profileId) });
    qc.invalidateQueries({ queryKey: keys.recommendations(profileId) });
    qc.invalidateQueries({ queryKey: keys.dynamic(profileId) });
  }, [qc, profileId]);

  /** After a source connects: for a real history import, show the confirmation (and land only once
   * the user continues); for a request-only source (Seerr, count 0) just refresh in place. */
  function handleConnected(importedCount: number) {
    if (importedCount > 0) {
      setSynced(importedCount);
    } else {
      invalidate();
    }
  }

  async function exploreSample() {
    setSeeding(true);
    try {
      await catalog.mutateAsync();
      // Landing is gated on history existing (App), and taste finishes in the background — so the
      // user drops into Browse (with its "building your profile" state) as soon as this resolves.
      await sample.mutateAsync();
    } catch {
      // Either seed can reject (e.g. 403 sample-disabled in prod). The mutation's isError drives the
      // ErrorState below; swallow here so the promise doesn't reject unhandled.
    }
  }

  // Auto-advance the confirmation after a short dwell so the user isn't forced to click through.
  useEffect(() => {
    if (synced === null) return;
    const timer = setTimeout(invalidate, CONFIRM_DWELL_MS);
    return () => clearTimeout(timer);
  }, [synced, invalidate]);

  if (synced !== null) {
    return (
      <main className={styles.cold} data-testid="sync-confirmation">
        <span className={styles.halo} aria-hidden="true">
          <BeaconGlyph />
        </span>
        <h1 className={styles.coldTitle}>{t("coldStart.synced.title", { count: synced })}</h1>
        <p className={styles.lede}>{t("coldStart.synced.lede")}</p>
        <button
          type="button"
          className={`btn btn-primary ${styles.cta}`}
          data-testid="sync-continue"
          onClick={invalidate}
        >
          {t("coldStart.synced.continue")}
        </button>
      </main>
    );
  }

  if (scratch) {
    // The by-hand seeding flow. Once it logs the loved picks + kicks off taste, invalidate so the
    // app (gated on history existing) reveals Browse — which handles the thin `profile_building`
    // profile honestly.
    return <ScratchStart profileId={profileId} onDone={invalidate} />;
  }

  if (seeding && !sample.isError && !catalog.isError) {
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

      {/* The always-available escape for a user with no Trakt/Plex/Jellyfin: seed a taste profile by
          hand. Not sample-gated — this is the only path that works without any of those three tools
          or a dev sample dataset (round 7, fix 1). */}
      <button
        type="button"
        className={styles.link}
        data-testid="start-from-scratch"
        onClick={() => setScratch(true)}
      >
        {t("coldStart.startFromScratch")}
      </button>

      {sampleAvailable && (
        <>
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

          {catalog.isError && <ErrorState error={catalog.error} />}
          {sample.isError && <ErrorState error={sample.error} />}
        </>
      )}

      <SourcePicker
        profileId={profileId}
        open={pickerOpen}
        onOpenChange={setPickerOpen}
        onConnected={handleConnected}
      />
    </main>
  );
}
