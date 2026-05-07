import {
  Accordion,
  AccordionContent,
  AccordionItem,
  AccordionTrigger,
} from "@/components/ui/accordion";
import { SuggestionCard } from "./suggestion-card";
import type { LoopSuggestionGroup } from "@/lib/types";

export function PendingTasksTab({
  groups,
}: {
  groups: LoopSuggestionGroup[];
}) {
  if (groups.length === 0) {
    return (
      <div className="p-6 text-center text-muted-foreground text-sm">
        No pending suggestions for this coordinator.
      </div>
    );
  }

  return (
    <Accordion defaultValue={groups.map((_, i) => i)} className="px-3 py-2">
      {groups.map((group, index) => {
        const key = group.loop_id || `unlinked-${index}`;
        const title = group.loop_title || "Unlinked Suggestions";
        const subtitle = [group.candidate_name, group.client_company]
          .filter(Boolean)
          .join(" — ");

        return (
          <AccordionItem key={key} value={index}>
            <AccordionTrigger className="text-sm hover:no-underline">
              <div className="flex items-center gap-2">
                <span className="font-medium">{title}</span>
                {subtitle && subtitle !== title && (
                  <span className="text-muted-foreground text-xs">
                    {subtitle}
                  </span>
                )}
                <span className="text-xs text-muted-foreground ml-auto mr-2">
                  ({group.suggestions.length})
                </span>
              </div>
            </AccordionTrigger>
            <AccordionContent>
              <div className="space-y-2">
                {group.suggestions.map((view) => (
                  <SuggestionCard key={view.suggestion.id} view={view} />
                ))}
              </div>
            </AccordionContent>
          </AccordionItem>
        );
      })}
    </Accordion>
  );
}
