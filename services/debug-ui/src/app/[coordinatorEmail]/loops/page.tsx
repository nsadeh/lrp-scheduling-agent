import { Suspense } from "react";
import { getCoordinatorIdByEmail } from "@/lib/queries/coordinators";
import {
  getAllLoopsForCoordinator,
  getActiveLoopsForCoordinator,
} from "@/lib/queries/loops";
import { LoopsTab } from "@/components/loops-tab";
import { DetailPanel } from "@/components/detail-panel";

export default async function LoopsPage({
  params,
  searchParams,
}: {
  params: Promise<{ coordinatorEmail: string }>;
  searchParams: Promise<{ loopId?: string; showAll?: string; q?: string }>;
}) {
  const { coordinatorEmail: rawEmail } = await params;
  const coordinatorEmail = decodeURIComponent(rawEmail);
  const { loopId } = await searchParams;

  const coordinatorId = await getCoordinatorIdByEmail(coordinatorEmail);

  if (!coordinatorId) {
    return (
      <div className="p-6 text-muted-foreground text-sm">
        Coordinator not found in database.
      </div>
    );
  }

  const [allLoops, activeLoops] = await Promise.all([
    getAllLoopsForCoordinator(coordinatorId),
    getActiveLoopsForCoordinator(coordinatorId),
  ]);

  return (
    <div className="flex h-full min-h-0">
      {/* Left: loops list */}
      <div className="w-[480px] shrink-0 overflow-y-auto border-r">
        <LoopsTab allLoops={allLoops} activeLoops={activeLoops} />
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
            Select a loop to see details.
          </div>
        )}
      </div>
    </div>
  );
}
