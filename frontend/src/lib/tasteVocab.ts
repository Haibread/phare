/** Display-only translation of the closed taste vocabulary (TMDB genres + controlled affinity
 * descriptors) shown as chips on the Profile screen (review F1 / M8.3).
 *
 * The STORED value is always the canonical English key — affinity matching, taste overrides, and
 * everything sent to the backend key on it. So we translate purely at display time and never touch
 * the value: a chip shown as "Horreur" is still stored, edited, and removed as "Horror". Free-form
 * chips (the LLM's natural-language `likes`) aren't in this table and render exactly as stored.
 *
 * ⚠️ Hand-mirrored from the backend's closed vocabulary in
 * `backend/src/phare/recommend/genres.py` — `_GENRE_TRANSLATIONS["fr"]` (genres) plus a French
 * rendering of `AFFINITY_KEYWORDS` (descriptors). If a term or language changes there, mirror it
 * here (and vice-versa); a missing entry just falls back to the English key, never an error.
 */

const FR_TASTE_TERMS: Record<string, string> = {
  // TMDB genres — mirror of `_GENRE_TRANSLATIONS["fr"]`.
  Action: "Action",
  Adventure: "Aventure",
  Animation: "Animation",
  Comedy: "Comédie",
  Crime: "Crime",
  Documentary: "Documentaire",
  Drama: "Drame",
  Family: "Familial",
  Fantasy: "Fantastique",
  History: "Histoire",
  Horror: "Horreur",
  Music: "Musique",
  Mystery: "Mystère",
  Romance: "Romance",
  "Science Fiction": "Science-Fiction",
  Thriller: "Thriller",
  War: "Guerre",
  Western: "Western",
  "Sci-Fi & Fantasy": "Science-Fiction & Fantastique",
  "War & Politics": "Guerre & Politique",
  "Action & Adventure": "Action & Aventure",
  Kids: "Enfants",
  Reality: "Téléréalité",
  // Controlled affinity descriptors — French rendering of `AFFINITY_KEYWORDS`.
  "slow-burn": "rythme lent",
  "fast-paced": "rythmé",
  atmospheric: "atmosphérique",
  cerebral: "cérébral",
  "mind-bending": "vertigineux",
  "character-driven": "centré sur les personnages",
  "plot-driven": "centré sur l'intrigue",
  dark: "sombre",
  "feel-good": "réconfortant",
  violent: "violent",
  gory: "gore",
  wholesome: "bon enfant",
  romantic: "romantique",
  nostalgic: "nostalgique",
  stylish: "stylé",
  gritty: "âpre",
  epic: "épique",
  minimalist: "minimaliste",
  franchise: "franchise",
  blockbuster: "blockbuster",
  indie: "indépendant",
  arthouse: "cinéma d'auteur",
  cult: "culte",
  prestige: "prestige",
  campy: "kitsch",
  psychological: "psychologique",
  dystopian: "dystopique",
  "coming-of-age": "récit d'apprentissage",
  "true-story": "histoire vraie",
  ensemble: "film choral",
};

// Case-insensitive index: stored keys mostly match the vocabulary casing, but the extractor LLM can
// return a genre lower-cased ("horror"), so fall back to a normalized lookup before giving up.
const FR_TASTE_TERMS_LC: Record<string, string> = Object.fromEntries(
  Object.entries(FR_TASTE_TERMS).map(([key, value]) => [key.toLowerCase(), value]),
);

const TABLES: Record<string, Record<string, string>> = { fr: FR_TASTE_TERMS };
const TABLES_LC: Record<string, Record<string, string>> = { fr: FR_TASTE_TERMS_LC };

/** Localize a stored (English) taste-vocabulary term for display. English (or any language without a
 * table) and any free-form term outside the closed vocabulary return unchanged. The input value is
 * never mutated — callers keep using it as the canonical key. */
export function translateTasteTerm(value: string, language: string): string {
  const lang = language.split("-")[0] ?? language; // "fr-FR" -> "fr"
  const table = TABLES[lang];
  if (table === undefined) {
    return value;
  }
  return table[value] ?? TABLES_LC[lang]?.[value.toLowerCase()] ?? value;
}
