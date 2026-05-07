"use client";

import { useState } from "react";
import { useRouter, useSearchParams } from "next/navigation";
import { Badge } from "@/components/ui/badge";
import { StageBadge } from "./stage-badge";
import { DraftPreview } from "./draft-preview";
import { JsonViewer } from "./json-viewer";
import type { SuggestionWithContext } from "@/lib/types";

const actionColors: Record<string, string> = {
  draft_email: "bg-blue-100 text-blue-800",
  advance_stage: "bg-green-100 text-green-800",
  create_loop: "bg-purple-100 text-purple-800",
  link_thread: "bg-orange-100 text-orange-800",
  ask_coordinator: "bg-yellow-100 text-yellow-800",
};

export function SuggestionCard({ view }: { view: SuggestionWithContext }) {
  const [expanded, setExpanded] = useState(false);
  const router = useRouter();
  const searchParams = useSearchParams();
  const { suggestion, draft } = view;

  const handleSelect = () => {
    if (suggestion.loop_id) {
      const params = new URLSearchParams(searchParams.toString());
      params.set("loopId", suggestion.loop_id);
      router.push(`?${params.toString()}`, { scroll: false });
    }
  };

  return (
    <div
      className="border rounded-lg p-3 hover:bg-muted/50 transition-colors cursor-pointer"
      onClick={handleSelect}
    >
      <div className="flex items-start justify-between gap-2">
        <div className="flex-1 min-w-0">
          <div className="flex items-center gap-2 flex-wrap">
            <Badge
              variant="outline"
              className={actionColors[suggestion.action] || ""}
            >
              {suggestion.action.replace(/_/g, " ")}
            </Badge>
            <Badge variant="secondary" className="text-xs">
              {suggestion.classification.replace(/_/g, " ")}
            </Badge>
            {view.loop_state && <StageBadge state={view.loop_state} />}
            <span className="text-xs text-muted-foreground">
              {Math.round(suggestion.confidence * 100)}%
            </span>
          </div>
          <p className="mt-1 text-sm">{suggestion.summary}</p>
        </div>
        <button
          onClick={(e) => {
            e.stopPropagation();
            setExpanded(!expanded);
          }}
          className="text-muted-foreground hover:text-foreground text-xs shrink-0"
        >
          {expanded ? "▾ Less" : "▸ More"}
        </button>
      </div>

      {/* Draft is always visible for draft_email — shows recipient + body */}
      {draft && (
        <div className="mt-3" onClick={(e) => e.stopPropagation()}>
          <DraftPreview draft={draft} />
        </div>
      )}

      {expanded && (
        <div
          className="mt-3 space-y-2 border-t pt-3"
          onClick={(e) => e.stopPropagation()}
        >
          {suggestion.reasoning && (
            <div className="text-xs">
              <span className="font-medium">Reasoning: </span>
              <span className="text-muted-foreground">
                {suggestion.reasoning}
              </span>
            </div>
          )}
          <JsonViewer data={suggestion.action_data} label="Action Data" />
          <div className="flex items-center gap-4 text-xs text-muted-foreground">
            <span>Created: {new Date(suggestion.created_at).toLocaleString()}</span>
          </div>
          <div className="flex items-center gap-4 text-xs text-muted-foreground">
            {view.recruiter_name && (
              <span>Recruiter: {view.recruiter_name}</span>
            )}
            {view.client_contact_name && (
              <span>Client: {view.client_contact_name}</span>
            )}
            {view.client_manager_name && (
              <span>CM: {view.client_manager_name}</span>
            )}
          </div>
        </div>
      )}
    </div>
  );
}
