import type { ReactNode } from "react";
import { NavLink, Outlet } from "react-router-dom";
import {
  Briefcase,
  ClipboardList,
  GitBranch,
  LayoutDashboard,
  Mail,
  Settings,
  UserRound,
  Users,
  type LucideIcon,
} from "lucide-react";
import { cn } from "@/lib/utils";

const NAV: { to: string; label: string; icon: LucideIcon }[] = [
  { to: "/", label: "Dashboard", icon: LayoutDashboard },
  { to: "/jobs", label: "Jobs", icon: Briefcase },
  { to: "/applications", label: "Applications", icon: ClipboardList },
  { to: "/intervention", label: "Intervention", icon: Users },
  { to: "/pipeline", label: "Pipeline", icon: GitBranch },
  { to: "/summary", label: "Summary", icon: Mail },
  { to: "/setup", label: "Setup", icon: UserRound },
  { to: "/settings", label: "Settings", icon: Settings },
];

export default function Layout() {
  return (
    <div className="grid min-h-screen grid-cols-[236px_1fr] bg-background">
      <aside className="sticky top-0 flex h-screen flex-col gap-1 border-r border-border bg-sidebar px-3 py-5">
        <div className="flex items-center gap-2.5 px-2 pb-4">
          <div className="grid size-8 shrink-0 place-items-center rounded-lg bg-primary text-[13px] font-bold text-primary-foreground">
            JA
          </div>
          <div className="leading-tight">
            <div className="text-sm font-semibold tracking-tight">Job Applier</div>
            <div className="text-xs text-muted-foreground">Agent console</div>
          </div>
        </div>

        <nav className="flex flex-col gap-0.5">
          {NAV.map(({ to, label, icon: Icon }) => (
            <NavLink
              key={to}
              to={to}
              end={to === "/"}
              className={({ isActive }) =>
                cn(
                  "flex items-center gap-2.5 rounded-md px-2.5 py-2 text-[13.5px] font-medium transition-colors",
                  isActive
                    ? "bg-accent text-accent-foreground"
                    : "text-muted-foreground hover:bg-accent/60 hover:text-foreground",
                )
              }
            >
              <Icon className="size-4 shrink-0" strokeWidth={2} />
              {label}
            </NavLink>
          ))}
        </nav>

        <div className="mt-auto border-t border-border px-2 pt-3 text-xs text-muted-foreground">
          Local · autonomous applications
        </div>
      </aside>

      <main className="min-w-0 px-8 pb-16 pt-7">
        <Outlet />
      </main>
    </div>
  );
}

export function PageHeader({
  title,
  subtitle,
  actions,
}: {
  title: string;
  subtitle?: string;
  actions?: ReactNode;
}) {
  return (
    <header className="mb-6 flex items-start justify-between gap-4">
      <div className="flex flex-col gap-1">
        <h1 className="text-[22px] font-semibold tracking-tight">{title}</h1>
        {subtitle && <p className="text-[13px] text-muted-foreground">{subtitle}</p>}
      </div>
      {actions && <div className="flex flex-wrap items-center gap-2">{actions}</div>}
    </header>
  );
}
