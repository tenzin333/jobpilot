import { Link } from "react-router-dom";
import { ArrowUpRight, Loader2, RefreshCw } from "lucide-react";
import { PageHeader } from "@/components/Layout";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { api, type DashboardData } from "@/lib/api";
import { usePolling } from "@/lib/hooks";
import { cn } from "@/lib/utils";

const STAT_CARDS: {
  key: keyof DashboardData["stats"];
  label: string;
  accent?: string;
}[] = [
  { key: "jobs", label: "Jobs discovered" },
  { key: "ranked", label: "Ranked" },
  { key: "tailored", label: "Tailored" },
  { key: "queued", label: "Queued (dry-run)" },
  { key: "submitted", label: "Submitted", accent: "text-success" },
  { key: "needs_human", label: "Needs you", accent: "text-warning" },
  { key: "failed", label: "Failed", accent: "text-destructive" },
];

export default function Dashboard() {
  const { data, error, loading, refresh } = usePolling(api.dashboard, 4000);

  if (loading) {
    return (
      <>
        <PageHeader title="Dashboard" />
        <div className="grid grid-cols-[repeat(auto-fill,minmax(150px,1fr))] gap-3">
          {Array.from({ length: 7 }).map((_, i) => (
            <Skeleton key={i} className="h-[92px] rounded-xl" />
          ))}
        </div>
      </>
    );
  }

  if (error || !data) {
    return (
      <>
        <PageHeader title="Dashboard" />
        <Card>
          <CardContent className="py-10 text-center">
            <p className="font-medium">Couldn’t reach the agent</p>
            <p className="mt-1 text-sm text-muted-foreground">{error ?? "No data."}</p>
            <Button variant="outline" size="sm" className="mt-4" onClick={refresh}>
              Retry
            </Button>
          </CardContent>
        </Card>
      </>
    );
  }

  const { stats, settings, profile_configured, pipeline } = data;

  return (
    <>
      <PageHeader
        title="Dashboard"
        subtitle="Overview of your discovery and application pipeline."
        actions={
          <Button variant="outline" size="sm" onClick={refresh}>
            <RefreshCw /> Refresh
          </Button>
        }
      />

      {pipeline.running && (
        <Alert className="mb-4">
          <Loader2 className="animate-spin" />
          <AlertDescription>
            Pipeline running · <span className="font-medium text-foreground">{pipeline.stage}</span> —{" "}
            <Link to="/pipeline" className="font-medium text-foreground underline underline-offset-4">
              view live
            </Link>
          </AlertDescription>
        </Alert>
      )}

      {!profile_configured && (
        <Alert className="mb-4 border-warning/40 text-warning">
          <AlertDescription className="text-warning/90">
            No profile yet.{" "}
            <Link to="/setup" className="font-medium text-warning underline underline-offset-4">
              Set up your résumé and preferences →
            </Link>
          </AlertDescription>
        </Alert>
      )}

      <div className="mb-5 grid grid-cols-[repeat(auto-fill,minmax(150px,1fr))] gap-3">
        {STAT_CARDS.map((c) => (
          <Card key={c.key} className="gap-0 py-0">
            <CardContent className="px-4 py-4">
              <div className={cn("text-[28px] font-semibold leading-none tracking-tight", c.accent)}>
                {stats[c.key]}
              </div>
              <div className="mt-1.5 text-[12.5px] text-muted-foreground">{c.label}</div>
            </CardContent>
          </Card>
        ))}
      </div>

      <div className="grid grid-cols-1 items-start gap-4 lg:grid-cols-[1.4fr_1fr]">
        <Card className="gap-4 py-5">
          <CardHeader className="px-5">
            <CardTitle className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
              Quick actions
            </CardTitle>
          </CardHeader>
          <CardContent className="flex flex-wrap gap-2 px-5">
            <Button asChild size="sm">
              <Link to="/jobs">Discover &amp; rank</Link>
            </Button>
            <Button asChild variant="outline" size="sm">
              <Link to="/matches">Review matches</Link>
            </Button>
            <Button asChild variant="outline" size="sm">
              <Link to="/intervention">Intervention ({stats.needs_human})</Link>
            </Button>
            <Button asChild variant="ghost" size="sm">
              <Link to="/pipeline">
                Run pipeline <ArrowUpRight />
              </Link>
            </Button>
          </CardContent>
        </Card>

        <Card className="gap-4 py-5">
          <CardHeader className="px-5">
            <CardTitle className="text-xs font-semibold uppercase tracking-wider text-muted-foreground">
              Status
            </CardTitle>
          </CardHeader>
          <CardContent className="px-5">
            <Row label="Dry run">
              <Badge variant={settings.dry_run ? "default" : "secondary"}>
                {settings.dry_run ? "On" : "Off"}
              </Badge>
            </Row>
            <Row label="Kill switch">
              <Badge variant={settings.submit_kill_switch ? "destructive" : "secondary"}>
                {settings.submit_kill_switch ? "Engaged" : "Off"}
              </Badge>
            </Row>
            <Row label="Daily cap">
              <span className="text-[13px] font-medium">{settings.daily_submit_cap}</span>
            </Row>
            <Row label="Match threshold">
              <span className="text-[13px] font-medium">{settings.match_threshold}</span>
            </Row>
            <Row label="Profile" last>
              <Badge variant={profile_configured ? "success" : "warning"}>
                {profile_configured ? "Configured" : "Not set up"}
              </Badge>
            </Row>
          </CardContent>
        </Card>
      </div>
    </>
  );
}

function Row({
  label,
  children,
  last,
}: {
  label: string;
  children: React.ReactNode;
  last?: boolean;
}) {
  return (
    <div
      className={cn(
        "flex items-center justify-between gap-3 py-2.5",
        !last && "border-b border-border",
      )}
    >
      <span className="text-[13px] text-muted-foreground">{label}</span>
      {children}
    </div>
  );
}
