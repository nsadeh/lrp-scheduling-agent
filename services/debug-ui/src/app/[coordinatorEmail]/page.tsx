import { Suspense } from "react";
import {
  getPendingSuggestionsWithContext,
  groupByLoop,
} from "@/lib/queries/suggestions";
import { PendingTasksTab } from "@/components/pending-tasks-tab";
import { DetailPanel } from "@/components/detail-panel";

export default async function PendingTasksPage({
  params,
  searchParams,
}: {
  params: Promise<{ coordinatorEmail: string }>;
  searchParams: Promise<{ loopId?: string }>;
}) {
  const { coordinatorEmail: rawEmail } = await params;
  const coordinatorEmail = decodeURIComponent(rawEmail);
  const { loopId } = await searchParams;

  const views = await getPendingSuggestionsWithContext(coordinatorEmail);
  const groups = groupByLoop(views);

  return (
    <div className="flex h-full min-h-0">
      {/* Left: pending tasks */}
      <div className="w-[480px] shrink-0 overflow-y-auto border-r">
        <PendingTasksTab groups={groups} />
      </div>

      {/* Right: detail panel */}
      <div className="flex-1 overflow-hidden">
        {loopId ? (
          <Suspense
            fallback={
              <div className="p-4 text-sm text-muted-foreground">
                Loading loop details...
              </div>
            }
          >
            <DetailPanel
              loopId={loopId}
              coordinatorEmail={coordinatorEmail}
            />
          </Suspense>
        ) : (
          <div className="flex items-center justify-center h-full text-muted-foreground text-sm">
            Select a task or loop to see details.
          </div>
        )}
      </div>
    </div>
  );
}
