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

// --- Trace / Observability ---

export interface ToolCall {
  name: string;
  args: Record<string, unknown>;
  id?: string;
}

export interface TraceMessage {
  type: "human" | "ai" | "tool";
  content?: string;
  content_text?: string;
  tool_calls?: ToolCall[];
  name?: string;
  status?: string;
}

export interface Trace {
  _id: string;
  trace_id?: string;
  langsmith_run_id?: string;
  associate_id: string;
  associate_name: string;
  message_id: string;
  correlation_id?: string;
  entity_type: string;
  entity_id: string;
  run_type: string;
  messages: TraceMessage[];
  tags: string[];
  prompt_tokens: number;
  completion_tokens: number;
  total_tokens: number;
  total_cost?: number;
  start_time?: string;
  end_time?: string;
  duration_ms?: number;
  execution_status: "success" | "error" | "cancelled";
  error?: string;
  status: "created" | "evaluated";
  feedback_stats: Record<string, { passed?: boolean }>;
  created_at: string;
}

export interface RubricScore {
  rule_id: string;
  rule_name?: string;
  severity?: string;
  passed: boolean;
  score: number;
  reasoning?: string;
  failure_attribution?: string;
  recommendation?: string;
}

export interface OutcomeCheck {
  rule_id?: string;
  entity_type?: string;
  passed: boolean;
  reasoning?: string;
}

export interface EvaluationResult {
  _id: string;
  trace_id: string;
  associate_name: string;
  entity_type: string;
  entity_id: string;
  passed: boolean;
  rubric_passed: boolean;
  rubric_scores: RubricScore[];
  outcome_checks: OutcomeCheck[];
  status: string;
  created_at: string;
}

export interface ActivityBucket {
  timestamp: string;
  counts: Record<string, number>;
  errors: number;
  total: number;
}

export interface ActivitySummaryResponse {
  buckets: ActivityBucket[];
  total_count: number;
  error_count: number;
  associates: string[];
}
