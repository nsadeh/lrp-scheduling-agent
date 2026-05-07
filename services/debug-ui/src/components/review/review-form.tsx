"use client";

import { useState, useTransition } from "react";
import { useRouter } from "next/navigation";
import { Switch } from "@/components/ui/switch";
import { Textarea } from "@/components/ui/textarea";
import { Button } from "@/components/ui/button";
import { saveReviewAction } from "@/lib/reviews/actions";
import { ResetItemButton } from "@/components/review/reset-buttons";
import type { ReviewEntry, ReviewItemType } from "@/lib/reviews/types";

/**
 * The per-item review form. Lives at the bottom of each flow page.
 *
 * Defaults to "correct" (the agent is right most of the time, so this
 * minimizes friction). Only renders the "what / why" textareas when the
 * reviewer flips the switch off.
 */
export function ReviewForm({
  coordinatorEmail,
  date,
  suggestionId,
  itemType,
  initial,
  prevHref,
  nextHref,
  reportHref,
}: {
  coordinatorEmail: string;
  date: string;
  suggestionId: string;
  itemType: ReviewItemType;
  initial: ReviewEntry | null;
  prevHref: string | null;
  nextHref: string | null;
  reportHref: string;
}) {
  const router = useRouter();
  const [isPending, startTransition] = useTransition();

  const [correct, setCorrect] = useState<boolean>(initial?.correct ?? true);
  const [whatWrong, setWhatWrong] = useState<string>(
    initial?.what_was_wrong ?? ""
  );
  const [whyWrong, setWhyWrong] = useState<string>(
    initial?.why_incorrect ?? ""
  );

  const submit = (target: "next" | "prev" | "report") => {
    const entry: ReviewEntry = {
      suggestion_id: suggestionId,
      item_type: itemType,
      correct,
      what_was_wrong: correct ? "" : whatWrong.trim(),
      why_incorrect: correct ? "" : whyWrong.trim(),
      reviewed_at: new Date().toISOString(),
    };
    startTransition(async () => {
      await saveReviewAction({ coordinatorEmail, date, entry });
      if (target === "next" && nextHref) router.push(nextHref);
      else if (target === "prev" && prevHref) router.push(prevHref);
      else if (target === "report") router.push(reportHref);
      else router.refresh();
    });
  };

  return (
    <div className="border rounded p-4 space-y-4 bg-card">
      <div className="flex items-start justify-between gap-3">
        <label className="flex items-center justify-between gap-3 flex-1">
          <div>
            <div className="text-sm font-medium">
              Was this {labelFor(itemType)} correct?
            </div>
            <div className="text-xs text-muted-foreground">
              Default is yes. Flip if the agent got it wrong.
              {initial && (
                <>
                  {" · "}
                  <span className="italic">
                    Last saved {new Date(initial.reviewed_at).toLocaleString()}
                  </span>
                </>
              )}
            </div>
          </div>
          <div className="flex items-center gap-2">
            <span
              className={
                correct
                  ? "text-sm text-muted-foreground"
                  : "text-sm font-semibold text-destructive"
              }
            >
              {correct ? "Correct" : "Incorrect"}
            </span>
            <Switch checked={correct} onCheckedChange={setCorrect} />
          </div>
        </label>
        {initial && (
          <ResetItemButton
            coordinatorEmail={coordinatorEmail}
            date={date}
            suggestionId={suggestionId}
          />
        )}
      </div>

      {!correct && (
        <div className="space-y-3">
          <label className="block space-y-1">
            <span className="text-xs font-medium text-muted-foreground">
              What was wrong?
            </span>
            <Textarea
              value={whatWrong}
              onChange={(e) => setWhatWrong(e.target.value)}
              placeholder="The client should have been Cyberdyne, not Skynet..."
              rows={3}
            />
          </label>
          <label className="block space-y-1">
            <span className="text-xs font-medium text-muted-foreground">
              Why do you think it was decided that way?
            </span>
            <Textarea
              value={whyWrong}
              onChange={(e) => setWhyWrong(e.target.value)}
              placeholder="The thread had both companies in CC, so the agent picked the first one mentioned..."
              rows={3}
            />
          </label>
        </div>
      )}

      <div className="flex items-center justify-between pt-2">
        <Button
          variant="outline"
          disabled={!prevHref || isPending}
          onClick={() => submit("prev")}
        >
          ← Save &amp; Previous
        </Button>
        <div className="flex gap-2">
          <Button
            variant="ghost"
            disabled={isPending}
            onClick={() => submit("report")}
          >
            Save &amp; View Report
          </Button>
          <Button
            disabled={!nextHref || isPending}
            onClick={() => submit("next")}
          >
            {isPending ? "Saving…" : "Save & Next →"}
          </Button>
        </div>
      </div>
    </div>
  );
}

function labelFor(t: ReviewItemType): string {
  if (t === "create_loop") return "extraction";
  if (t === "draft_email") return "drafted email";
  return "question";
}
