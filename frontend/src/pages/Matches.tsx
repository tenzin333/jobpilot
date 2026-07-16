import { useEffect, useRef, useState } from "react";
import { ExternalLink, Loader2, Sparkles } from "lucide-react";
import { PageHeader } from "@/components/Layout";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { api, type JobRow, type StatusState } from "@/lib/api";
import { usePolling } from "@/lib/hooks";
import { toneVariant } from "@/lib/tone";
import { cn } from "@/lib/utils";

function scoreClasses(score: number | null): string {
  if (score === null) return "bg-muted text-muted-foreground";
  if (score >= 80) return "bg-success/12 text-success";
  if (score >= 60) return "bg-primary/8 text-foreground";
  return "bg-muted text-muted-foreground";
}

/** A single scored match; owns its own apply/poll state. */
function MatchCard({ job }: { job: JobRow }) {
  const app = job.application!; // Matches only renders scored jobs
  const [override, setOverride] = useState<StatusState | null>(null);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);
  const state = override ?? app.state;

  useEffect(() => {
    if (!state.polling) {
      if (timer.current) clearInterval(timer.current);
      return;
    }
    timer.current = setInterval(async () => {
      try {
        const next = await api.applyStatus(app.id);
        setOverride(next);
        if (!next.polling && timer.current) clearInterval(timer.current);
      } catch {
        /* keep last known state */
      }
    }, 2000);
    return () => {
      if (timer.current) clearInterval(timer.current);
    };
  }, [app.id, state.polling]);

  async function onApply(retry: boolean) {
    setOverride({ ...app.state, label: "Starting", polling: true, running: true });
    try {
      setOverride(await (retry ? api.retry(app.id) : api.apply(app.id)));
    } catch (e) {
      setOverride({ ...app.state, label: (e as Error).message, tone: "danger", polling: false });
    }
  }

  const canApply = app.can_apply && !state.polling;
  const isFailed = state.tone === "danger" && !state.polling;

  return (
    <Card className="gap-0 py-0">
      <CardContent className="flex items-center gap-4 px-4 py-3.5">
        <div
          className={cn(
            "grid size-[52px] shrink-0 place-items-center rounded-md text-[17px] font-semibold",
            scoreClasses(app.match_score),
          )}
        >
          {app.match_score ?? "—"}
        </div>

        <div className="min-w-0 flex-1">
          <div className="text-[14.5px] font-semibold">{job.title}</div>
          <div className="mt-0.5 flex flex-wrap items-center gap-2 text-[12.5px] text-muted-foreground">
            <span>{job.company}</span>
            <span className="text-border">·</span>
            <span>{job.location || "—"}</span>
            <span className="text-border">·</span>
            <Badge variant="outline" className="font-normal">
              {job.source}
            </Badge>
            {job.remote && <Badge variant="secondary">remote</Badge>}
          </div>
          {app.score_rationale && (
            <p className="mt-1.5 line-clamp-2 text-[12.5px] leading-snug text-muted-foreground">
              {app.score_rationale}
            </p>
          )}
          {job.apply_url && (
            <a
              href={job.apply_url}
              target="_blank"
              rel="noopener noreferrer"
              className="mt-1.5 inline-flex items-center gap-1 text-[12.5px] text-primary underline-offset-4 hover:underline"
            >
              View posting <ExternalLink className="size-3" />
            </a>
          )}
        </div>

        <div className="flex shrink-0 items-center gap-2">
          {canApply ? (
            <Button size="sm" onClick={() => onApply(false)}>
              Apply
            </Button>
          ) : isFailed ? (
            <>
              <Badge variant="destructive">{state.label}</Badge>
              <Button variant="outline" size="sm" onClick={() => onApply(true)}>
                Retry
              </Button>
            </>
          ) : state.polling ? (
            <Badge variant="secondary" className="gap-1.5">
              <Loader2 className="size-3 animate-spin" />
              {state.label}
              {state.elapsed ? ` (${state.elapsed}s)` : ""}
            </Badge>
          ) : (
            <Badge variant={toneVariant(state.tone)}>{state.label}</Badge>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

export default function Matches() {
  const { data, error, loading } = usePolling(api.jobs, null);
  const matches = (data?.jobs ?? []).filter((j) => j.application?.match_score != null);

  return (
    <>
      <PageHeader
        title="Matches"
        subtitle="Score-ranked matches. Apply to strong ones — we tailor, then submit."
      />

      {loading ? (
        <div className="flex flex-col gap-2.5">
          {Array.from({ length: 4 }).map((_, i) => (
            <Skeleton key={i} className="h-[84px] rounded-xl" />
          ))}
        </div>
      ) : error ? (
        <Card>
          <CardContent className="py-10 text-center">
            <p className="font-medium">Couldn’t load matches</p>
            <p className="mt-1 text-sm text-muted-foreground">{error}</p>
          </CardContent>
        </Card>
      ) : matches.length === 0 ? (
        <Card className="border-dashed">
          <CardContent className="py-14 text-center">
            <Sparkles className="mx-auto mb-2 size-5 text-muted-foreground" />
            <p className="font-medium">No scored matches yet</p>
            <p className="mt-1.5 text-sm text-muted-foreground">
              Discover and rank jobs first, then strong matches appear here.
            </p>
          </CardContent>
        </Card>
      ) : (
        <>
          <div className="mb-2.5 text-[12.5px] text-muted-foreground">
            {matches.length} match{matches.length === 1 ? "" : "es"}
          </div>
          <div className="flex flex-col gap-2.5">
            {matches.map((job) => (
              <MatchCard key={job.id} job={job} />
            ))}
          </div>
        </>
      )}
    </>
  );
}
