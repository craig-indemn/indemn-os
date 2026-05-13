export const ASSOCIATE_COLORS: Record<string, string> = {
  email_classifier: "#6C63FF",
  evaluator: "#B4AEFC",
  touchpoint_synthesizer: "#1B8F5A",
  intelligence_extractor: "#C4880A",
  meeting_classifier: "#D64545",
  slack_classifier: "#0891b2",
  company_enricher: "#7c3aed",
  proposal_hydrator: "#db2777",
  email_fetcher: "#64748b",
  meeting_fetcher: "#64748b",
  drive_fetcher: "#64748b",
  slack_fetcher: "#64748b",
  _default: "#9ca3af",
};

export const ERROR_COLOR = "#dc2626";

export function associateColor(name: string): string {
  const key = name.toLowerCase().replace(/\s+/g, "_");
  return ASSOCIATE_COLORS[key] || ASSOCIATE_COLORS._default;
}

export function associateAbbrev(name: string): string {
  return name
    .split(" ")
    .map((w) => w[0])
    .join("")
    .toUpperCase();
}
