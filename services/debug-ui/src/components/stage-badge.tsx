import { Badge } from "@/components/ui/badge";
import type { StageState } from "@/lib/types";

const stateStyles: Record<StageState, string> = {
  new: "bg-blue-100 text-blue-800",
  awaiting_candidate: "bg-yellow-100 text-yellow-800",
  awaiting_client: "bg-orange-100 text-orange-800",
  scheduled: "bg-green-100 text-green-800",
  complete: "bg-gray-100 text-gray-600",
  cold: "bg-slate-100 text-slate-500",
};

const stateLabels: Record<StageState, string> = {
  new: "New",
  awaiting_candidate: "Awaiting Candidate",
  awaiting_client: "Awaiting Client",
  scheduled: "Scheduled",
  complete: "Complete",
  cold: "Cold",
};

export function StageBadge({ state }: { state: StageState }) {
  return (
    <Badge variant="outline" className={stateStyles[state]}>
      {stateLabels[state]}
    </Badge>
  );
}
