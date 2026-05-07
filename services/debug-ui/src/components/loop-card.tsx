"use client";

import { useRouter, useSearchParams } from "next/navigation";
import { StageBadge } from "./stage-badge";
import { cn } from "@/lib/utils";
import type { LoopFull } from "@/lib/types";
import { NEXT_ACTIONS } from "@/lib/types";

export function LoopCard({ loop }: { loop: LoopFull }) {
  const router = useRouter();
  const searchParams = useSearchParams();
  const isSelected = searchParams.get("loopId") === loop.id;

  return (
    <div
      className={cn(
        "border rounded-lg p-3 cursor-pointer transition-colors",
        isSelected ? "bg-muted border-primary" : "hover:bg-muted/50"
      )}
      onClick={() => {
        const params = new URLSearchParams(searchParams.toString());
        params.set("loopId", loop.id);
        router.push(`?${params.toString()}`, { scroll: false });
      }}
    >
      <div className="flex items-center justify-between">
        <span className="text-sm font-medium">{loop.title}</span>
        <StageBadge state={loop.state} />
      </div>
      <div className="flex items-center gap-3 mt-1 text-xs text-muted-foreground">
        <span>{loop.candidate_name}</span>
        {loop.client_company && (
          <>
            <span>—</span>
            <span>{loop.client_company}</span>
          </>
        )}
      </div>
      <div className="text-xs text-muted-foreground mt-1">
        {NEXT_ACTIONS[loop.state]}
      </div>
      <div className="text-xs text-muted-foreground mt-1">
        Updated {new Date(loop.updated_at).toLocaleDateString()}
      </div>
    </div>
  );
}
