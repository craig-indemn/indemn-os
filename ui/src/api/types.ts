/** TypeScript types derived from API metadata. */

export interface FieldMeta {
  name: string;
  type: string;
  required: boolean;
  default: unknown;
  enum_values?: string[] | null;
  description?: string | null;
  is_state_field?: boolean;
  is_relationship?: boolean;
  relationship_target?: string | null;
  indexed?: boolean;
  unique?: boolean;
}

export interface CapabilityMeta {
  name: string;
  cli_command?: string;
}

export interface ExposedMethodMeta {
  name: string;
  cli_command?: string;
}

export interface EntityMeta {
  name: string;
  collection: string;
  is_kernel_entity: boolean;
  fields: FieldMeta[];
  state_machine: Record<string, string[]> | null;
  capabilities: CapabilityMeta[];
  exposed_methods: ExposedMethodMeta[];
  permissions: { read: boolean; write: boolean };
}

export interface EntityListMeta {
  name: string;
  fields: FieldMeta[];
  state_machine: Record<string, string[]> | null;
  capabilities: CapabilityMeta[];
  is_kernel_entity: boolean;
  exposed_methods: ExposedMethodMeta[];
  permissions: { read: boolean; write: boolean };
}

export interface QueueMessage {
  _id: string;
  entity_type: string;
  entity_id: string;
  event_type: string;
  target_role: string;
  status: string;
  correlation_id: string;
  summary?: { display?: string };
  created_at: string;
}

export interface AuthEvent {
  id: string;
  event_type: string;
  actor_id: string;
  entity_id: string;
  timestamp: string;
  metadata: Record<string, unknown>;
}

export interface HealthStatus {
  status: "healthy" | "degraded" | "unhealthy";
  checks: Record<string, string>;
}

export interface ChangeRecord {
  id: string;
  entity_type: string;
  entity_id: string;
  change_type: string;
  actor_id: string;
  timestamp: string;
  changes: { field: string; old_value: unknown; new_value: unknown }[];
  method?: string;
}
