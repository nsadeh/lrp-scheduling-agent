"use client";

import { useSearchParams, useRouter } from "next/navigation";
import { LoopCard } from "./loop-card";
import { SearchInput } from "./search-input";
import type { LoopFull } from "@/lib/types";

export function LoopsTab({
  allLoops,
  activeLoops,
}: {
  allLoops: LoopFull[];
  activeLoops: LoopFull[];
}) {
  const searchParams = useSearchParams();
  const router = useRouter();
  const showAll = searchParams.get("showAll") === "1";
  const query = (searchParams.get("q") || "").toLowerCase();

  const loops = showAll ? allLoops : activeLoops;
  const filtered = query
    ? loops.filter(
        (l) =>
          l.title.toLowerCase().includes(query) ||
          l.candidate_name.toLowerCase().includes(query) ||
          (l.client_company || "").toLowerCase().includes(query)
      )
    : loops;

  const toggleShowAll = () => {
    const params = new URLSearchParams(searchParams.toString());
    if (showAll) {
      params.delete("showAll");
    } else {
      params.set("showAll", "1");
    }
    router.push(`?${params.toString()}`, { scroll: false });
  };

  return (
    <div className="p-3 space-y-3">
      <div className="flex items-center gap-2">
        <div className="flex-1">
          <SearchInput />
        </div>
        <button
          onClick={toggleShowAll}
          className="text-xs px-2 py-1 border rounded-md hover:bg-muted transition-colors whitespace-nowrap"
        >
          {showAll ? "Active only" : "Show all"}
        </button>
      </div>

      <div className="text-xs text-muted-foreground">
        {filtered.length} loop{filtered.length !== 1 ? "s" : ""}
        {showAll ? " (all)" : " (active)"}
      </div>

      {filtered.length === 0 ? (
        <p className="text-sm text-muted-foreground text-center py-4">
          No loops found.
        </p>
      ) : (
        <div className="space-y-2">
          {filtered.map((loop) => (
            <LoopCard key={loop.id} loop={loop} />
          ))}
        </div>
      )}
    </div>
  );
}
