/** Re-export from api/hooks for backward compat with spec file structure. */
export { useEntityMeta } from "../api/hooks";

import { useAllEntityMeta } from "../api/hooks";

/**
 * Resolve a URL slug (e.g. "actionitems") to the real entity name ("ActionItem").
 * Uses the metadata endpoint as the source of truth — no string reconstruction.
 */
export function useEntityNameFromSlug(slug: string | undefined): string | null {
  const { data: allMeta } = useAllEntityMeta();
  if (!slug || !allMeta) return null;
  const match = allMeta.find(
    (e) => e.name.toLowerCase() + "s" === slug
  );
  return match?.name ?? null;
}
