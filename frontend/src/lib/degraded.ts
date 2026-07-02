import { createContext, useContext } from "react";

/** Page-level flag: retrieval is running on the local hash embedder (no embedding key), so
 * similarity — and therefore the fit labels — are only approximate. Provided by Browse (which
 * learns it from the recommendations response) and read by ConfidenceMeter, so a card deep in the
 * tree can cap its fit label without every row threading the flag through (review M2). */
export const EmbeddingsDegradedContext = createContext(false);

export function useEmbeddingsDegraded(): boolean {
  return useContext(EmbeddingsDegradedContext);
}
