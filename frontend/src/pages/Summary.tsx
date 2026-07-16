import { useState } from "react";
import { Loader2, Mail } from "lucide-react";
import { toast } from "sonner";
import { PageHeader } from "@/components/Layout";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { api, type SummaryData } from "@/lib/api";
import { usePolling } from "@/lib/hooks";
import { cn } from "@/lib/utils";

const TILES: { key: keyof SummaryData; label: string; accent?: string }[] = [
  { key: "discovered", label: "Discovered" },
  { key: "ranked", label: "Ranked" },
  { key: "tailored", label: "Tailored" },
  { key: "submitted", label: "Submitted", accent: "text-success" },
  { key: "failed_today", label: "Failed", accent: "text-destructive" },
  { key: "needs_human_today", label: "Needs you (new)", accent: "text-warning" },
  { key: "needs_human_open", label: "Needs you (open)", accent: "text-warning" },
];

function scoreBadge(score: number | null) {
  if (score === null) return "secondary";
  if (score >= 80) return "success";
  return "outline";
}

export default function Summary() {
  const { data, error, loading } = usePolling<SummaryData>(api.summary, null);
  const [emailing, setEmailing] = useState(false);

  async function onEmail() {
    setEmailing(true);
    try {
      await api.emailSummary();
      toast.success("Summary emailed");
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setEmailing(false);
    }
  }

  if (loading) {
    return (
      <>
        <PageHeader title="Summary" />
        <Skeleton className="mb-4 h-[92px] rounded-xl" />
        <Skeleton className="h-64 rounded-xl" />
      </>
    );
  }

  if (error || !data) {
    return (
      <>
        <PageHeader title="Summary" />
        <Card>
          <CardContent className="py-10 text-center">
            <p className="font-medium">Couldn’t load summary</p>
            <p className="mt-1 text-sm text-muted-foreground">{error ?? "No data."}</p>
          </CardContent>
        </Card>
      </>
    );
  }

  return (
    <>
      <PageHeader
        title="Summary"
        subtitle={`Daily digest for ${data.day}.`}
        actions={
          <Button
            size="sm"
            variant="outline"
            onClick={onEmail}
            disabled={emailing || !data.email_configured}
            title={data.email_configured ? undefined : "Configure SMTP_* in .env to enable email"}
          >
            {emailing ? <Loader2 className="animate-spin" /> : <Mail />}
            Email summary
          </Button>
        }
      />

      <div className="mb-4 grid grid-cols-[repeat(auto-fill,minmax(150px,1fr))] gap-3">
        {TILES.map((t) => (
          <Card key={t.key} className="gap-0 py-0">
            <CardContent className="px-4 py-4">
              <div className={cn("text-[28px] font-semibold leading-none tracking-tight", t.accent)}>
                {data[t.key] as number}
              </div>
              <div className="mt-1.5 text-[12.5px] text-muted-foreground">{t.label}</div>
            </CardContent>
          </Card>
        ))}
      </div>

      <Card>
        <CardHeader>
          <CardTitle>Top matches</CardTitle>
        </CardHeader>
        <CardContent>
          {data.top_matches.length === 0 ? (
            <p className="py-6 text-center text-sm text-muted-foreground">No matches yet.</p>
          ) : (
            <div className="flex flex-col divide-y divide-border">
              {data.top_matches.map((m, i) => (
                <div key={i} className="flex items-center gap-3 py-2.5">
                  <Badge variant={scoreBadge(m.score)} className="w-10 justify-center">
                    {m.score ?? "—"}
                  </Badge>
                  <div className="min-w-0 flex-1">
                    <div className="truncate text-[13.5px] font-medium">{m.title}</div>
                    <div className="truncate text-[12.5px] text-muted-foreground">{m.company}</div>
                  </div>
                  <Badge variant="outline" className="capitalize">
                    {m.status.replace(/_/g, " ")}
                  </Badge>
                </div>
              ))}
            </div>
          )}
        </CardContent>
      </Card>
    </>
  );
}
