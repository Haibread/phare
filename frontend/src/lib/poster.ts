/** Deterministic placeholder tint from a title id, so the text-fallback poster looks intentional
 * and stable across renders when there's no real artwork. */
export function posterTint(seed: string): string {
  let hash = 0;
  for (let i = 0; i < seed.length; i++) {
    hash = (hash * 31 + seed.charCodeAt(i)) % 360;
  }
  return `hsl(${hash} 38% 32%)`;
}
