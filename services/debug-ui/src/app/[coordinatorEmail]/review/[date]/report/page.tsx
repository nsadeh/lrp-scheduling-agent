import Link from "next/link";
import { notFound } from "next/navigation";
import { getDailyReviewItems } from "@/lib/queries/daily-review";
import { getCoordinatorEmails } from "@/lib/queries/coordinators";
import { readDayReview } from "@/lib/reviews/storage";
import { formatETDate } from "@/lib/reviews/time";
import { Report } from "@/components/review/report";
import { PrintButton } from "@/components/review/print-button";
import { buttonVariants } from "@/components/ui/button";

/**
 * Printable daily-review report.
 *
 * URL: /[coordinatorEmail]/review/[date]/report
 *
 * Fetches the same data as the flow page (suggestions for the ET day +
 * the JSON review file) and renders the formatted PDF-ready report.
 * Browser-print friendly: nav and the Print button are hidden via the
 * `data-print-hide` attribute and CSS rules in globals.css.
 */
export default async function ReviewReportPage({
  params,
}: {
  params: Promise<{ coordinatorEmail: string; date: string }>;
}) {
  const { coordinatorEmail: rawEmail, date } = await params;
  const coordinatorEmail = decodeURIComponent(rawEmail);

  if (!/^\d{4}-\d{2}-\d{2}$/.test(date)) notFound();

  const [items, dayReview, coordinators] = await Promise.all([
    getDailyReviewItems(coordinatorEmail, date),
    readDayReview(coordinatorEmail, date),
    getCoordinatorEmails(),
  ]);

  const coordinator =
    coordinators.find(
      (c) => c.email.toLowerCase() === coordinatorEmail.toLowerCase()
    ) ?? null;

  const formattedDate = formatETDate(date);
  const generatedDate = new Intl.DateTimeFormat("en-US", {
    timeZone: "America/New_York",
    year: "numeric",
    month: "long",
    day: "numeric",
  }).format(new Date());

  return (
    <div className="overflow-y-auto h-full bg-muted/40">
      {/* Top action bar — hidden in print */}
      <div
        data-print-hide
        className="sticky top-0 bg-background border-b z-10 px-6 py-3 flex items-center justify-between"
      >
        <Link
          href={`/${encodeURIComponent(coordinatorEmail)}/review/${date}`}
          className="text-sm text-muted-foreground hover:text-foreground"
        >
          ← Back to review flow
        </Link>
        <div className="flex gap-2">
          <Link
            href={`/${encodeURIComponent(coordinatorEmail)}/review`}
            className={buttonVariants({ variant: "outline" })}
          >
            All days
          </Link>
          <PrintButton />
        </div>
      </div>

      {/* The report itself */}
      <Report
        coordinatorEmail={coordinatorEmail}
        coordinatorName={coordinator?.name ?? null}
        date={date}
        formattedDate={formattedDate}
        generatedDate={generatedDate}
        items={items}
        reviews={dayReview}
      />
    </div>
  );
}
