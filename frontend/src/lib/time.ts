/** Locale-aware relative time ("2 hours ago" / "il y a 2 heures"), via the built-in
 * `Intl.RelativeTimeFormat` (no dependency). Used where the phrasing is user-facing prose and must
 * localize; the compact `relativeTime` above stays for the terse English-context sync labels. */
export function relativeTimeLocalized(iso: string, locale: string): string {
  const fmt = new Intl.RelativeTimeFormat(locale, { numeric: "auto" });
  const secs = Math.round((new Date(iso).getTime() - Date.now()) / 1000); // negative = in the past
  const abs = Math.abs(secs);
  if (abs < 60) return fmt.format(Math.round(secs), "second");
  const mins = secs / 60;
  if (Math.abs(mins) < 60) return fmt.format(Math.round(mins), "minute");
  const hrs = mins / 60;
  if (Math.abs(hrs) < 24) return fmt.format(Math.round(hrs), "hour");
  const days = hrs / 24;
  if (Math.abs(days) < 30) return fmt.format(Math.round(days), "day");
  const months = days / 30;
  if (Math.abs(months) < 12) return fmt.format(Math.round(months), "month");
  return fmt.format(Math.round(months / 12), "year");
}

/** Compact relative time ("2h ago", "3d ago") for sync timestamps. */
export function relativeTime(iso: string): string {
  const secs = Math.round((Date.now() - new Date(iso).getTime()) / 1000);
  if (secs < 60) return "just now";
  const mins = Math.round(secs / 60);
  if (mins < 60) return `${mins}m ago`;
  const hrs = Math.round(mins / 60);
  if (hrs < 24) return `${hrs}h ago`;
  const days = Math.round(hrs / 24);
  if (days < 30) return `${days}d ago`;
  const months = Math.round(days / 30);
  if (months < 12) return `${months}mo ago`;
  return `${Math.round(months / 12)}y ago`;
}
