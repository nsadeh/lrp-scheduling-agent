import { redirect } from "next/navigation";
import { getCoordinatorEmails } from "@/lib/queries/coordinators";

export default async function RootPage() {
  const coordinators = await getCoordinatorEmails();

  if (coordinators.length > 0) {
    redirect(`/${encodeURIComponent(coordinators[0].email)}`);
  }

  return (
    <div className="flex items-center justify-center h-screen">
      <p className="text-muted-foreground">
        No coordinators found in gmail_tokens table.
      </p>
    </div>
  );
}
