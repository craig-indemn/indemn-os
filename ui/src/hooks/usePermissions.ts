import type { EntityMeta } from "../api/types";

/** Permission-aware rendering helpers. */
export function canRead(meta: EntityMeta | undefined): boolean {
  return meta?.permissions.read ?? false;
}

export function canWrite(meta: EntityMeta | undefined): boolean {
  return meta?.permissions.write ?? false;
}
