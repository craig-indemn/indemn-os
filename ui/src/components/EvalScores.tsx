import { Badge } from "@/components/ui/badge";
import { Card, CardContent } from "@/components/ui/card";
import { Separator } from "@/components/ui/separator";

interface RubricScore {
  rule_id: string;
  rule_name?: string;
  severity?: string;
  passed: boolean;
  score: number;
  reasoning?: string;
  failure_attribution?: string;
  recommendation?: string;
}

interface OutcomeCheck {
  rule_id?: string;
  entity_type?: string;
  passed: boolean;
  reasoning?: string;
}

interface EvalScoresProps {
  result: Record<string, unknown>;
}

export function EvalScores({ result }: EvalScoresProps) {
  const rubricScores = (result.rubric_scores || []) as RubricScore[];
  const outcomeChecks = (result.outcome_checks || []) as OutcomeCheck[];
  const passed = result.passed as boolean;
  const rubricPassed = result.rubric_passed as boolean;

  return (
    <div className="space-y-4">
      <div className="flex items-center gap-2">
        <span className="text-xs font-semibold uppercase tracking-wide text-gray-400">
          Rubric Scores
        </span>
        <Badge variant={rubricPassed ? "outline" : "destructive"} className={rubricPassed ? "text-green-700 border-green-300 bg-green-50" : ""}>
          {rubricScores.filter((s) => s.passed).length}/{rubricScores.length} passed
        </Badge>
      </div>

      <div className="space-y-2">
        {rubricScores.map((score, i) => (
          <Card
            key={score.rule_id || i}
            className={`${score.passed ? "border-l-4 border-l-green-600" : "border-l-4 border-l-red-500"}`}
          >
            <CardContent className="p-3">
              <div className="flex items-center justify-between mb-1">
                <span className="text-xs font-semibold font-mono">{score.rule_id}</span>
                <div className="flex items-center gap-1.5">
                  {score.severity && (
                    <span className="text-[10px] text-gray-400 uppercase">{score.severity}</span>
                  )}
                  <Badge
                    variant={score.passed ? "outline" : "destructive"}
                    className={score.passed ? "text-green-700 border-green-300 bg-green-50 text-[10px]" : "text-[10px]"}
                  >
                    {score.passed ? "Pass" : "Fail"}
                  </Badge>
                </div>
              </div>
              {score.reasoning && (
                <p className="text-xs text-gray-600">{score.reasoning}</p>
              )}
              {score.failure_attribution && (
                <p className="text-[11px] text-amber-700 mt-1">
                  Attribution: {score.failure_attribution}
                </p>
              )}
              {score.recommendation && (
                <p className="text-[11px] text-indigo-600 mt-0.5">
                  Recommendation: {score.recommendation}
                </p>
              )}
            </CardContent>
          </Card>
        ))}
      </div>

      {outcomeChecks.length > 0 && (
        <>
          <Separator />
          <div>
            <span className="text-xs font-semibold uppercase tracking-wide text-gray-400">
              Outcome Checks
            </span>
            <div className="space-y-2 mt-2">
              {outcomeChecks.map((check, i) => (
                <Card
                  key={check.rule_id || i}
                  className={`${check.passed ? "border-l-4 border-l-green-600" : "border-l-4 border-l-red-500"}`}
                >
                  <CardContent className="p-3">
                    <div className="flex items-center justify-between mb-1">
                      <span className="text-xs font-semibold font-mono">
                        {check.rule_id || "outcome"}
                      </span>
                      <Badge
                        variant={check.passed ? "outline" : "destructive"}
                        className={check.passed ? "text-green-700 border-green-300 bg-green-50 text-[10px]" : "text-[10px]"}
                      >
                        {check.passed ? "Pass" : "Fail"}
                      </Badge>
                    </div>
                    {check.reasoning && (
                      <p className="text-xs text-gray-600 font-mono">{check.reasoning}</p>
                    )}
                  </CardContent>
                </Card>
              ))}
            </div>
          </div>
        </>
      )}

      <Separator />
      <div className="space-y-1">
        <span className="text-xs font-semibold uppercase tracking-wide text-gray-400">
          Result
        </span>
        <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs">
          <span className="text-gray-400">Overall</span>
          <span className={passed ? "text-green-700 font-medium" : "text-red-600 font-medium"}>
            {passed ? "Passed" : "Failed"}
          </span>
          <span className="text-gray-400">Result ID</span>
          <span className="font-mono text-indigo-600">{String(result._id || "").slice(0, 8)}</span>
          <span className="text-gray-400">Status</span>
          <span className="text-gray-600">{String(result.status || "")}</span>
        </div>
      </div>
    </div>
  );
}
