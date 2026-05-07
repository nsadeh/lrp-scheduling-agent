import { getCoordinatorEmails } from "@/lib/queries/coordinators";
import { CoordinatorSelector } from "@/components/coordinator-selector";
import { LeftPanel } from "@/components/left-panel";

export default async function CoordinatorLayout({
  children,
  params,
}: {
  children: React.ReactNode;
  params: Promise<{ coordinatorEmail: string }>;
}) {
  const { coordinatorEmail: rawEmail } = await params;
  const coordinatorEmail = decodeURIComponent(rawEmail);
  const coordinators = await getCoordinatorEmails();

  return (
    <div className="flex flex-col h-screen">
      {/* Top bar */}
      <div className="flex items-center justify-between px-4 py-2 border-b bg-background shrink-0">
        <h1 className="text-sm font-semibold">LRP Debug Dashboard</h1>
        <CoordinatorSelector
          coordinators={coordinators}
          currentEmail={coordinatorEmail}
        />
      </div>

      {/* Main content: children fills both left + right */}
      <div className="flex-1 min-h-0">
        <LeftPanel coordinatorEmail={coordinatorEmail}>
          {children}
        </LeftPanel>
      </div>
    </div>
  );
}
