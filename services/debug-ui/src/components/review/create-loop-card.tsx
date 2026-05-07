import type { DailyReviewItem } from "@/lib/queries/daily-review";
import { AgentJustification } from "./agent-justification";
import { ActorList, joinNameEmail, loopActorRows } from "./actor-list";

/**
 * Renders the agent's `create_loop` extraction side-by-side with the
 * loop that was actually created. The reviewer can compare extracted
 * fields against either the resolved loop or the originating thread.
 *
 * action_data shape (from CreateLoopExtraction):
 *   { candidate_name, client_name, client_email, client_company,
 *     cm_name, cm_email, recruiter_name, recruiter_email }
 */
export function CreateLoopCard({ item }: { item: DailyReviewItem }) {
  const data = (item.action_data ?? {}) as Record<string, string | null>;

  const extracted = [
    { label: "Candidate", value: data.candidate_name },
    {
      label: "Client",
      value: joinNameEmail(data.client_name, data.client_email),
      sub: data.client_company,
    },
    {
      label: "Recruiter",
      value: joinNameEmail(data.recruiter_name, data.recruiter_email),
    },
    { label: "CM", value: joinNameEmail(data.cm_name, data.cm_email) },
  ];

  return (
    <div className="space-y-4">
      <AgentJustification item={item} />
      <div className="grid grid-cols-2 gap-4">
        <ActorList title="Extracted by agent" rows={extracted} />
        <ActorList title="Resolved loop" rows={loopActorRows(item)} />
      </div>
    </div>
  );
}
