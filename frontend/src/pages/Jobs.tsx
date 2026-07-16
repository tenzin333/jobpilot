import { useEffect, useRef, useState } from "react";
import { ExternalLink, Loader2, Search, Trash2 } from "lucide-react";
import { PageHeader } from "@/components/Layout";
import { Alert, AlertDescription } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { api, type JobRow, type SourceInfo, type StatusState } from "@/lib/api";
import { usePolling } from "@/lib/hooks";
import { toneVariant } from "@/lib/tone";
import { cn } from "@/lib/utils";

function scoreClasses(score: number | null): string {
  if (score === null) return "bg-muted text-muted-foreground";
  if (score >= 80) return "bg-success/12 text-success";
  if (score >= 60) return "bg-primary/8 text-foreground";
  return "bg-muted text-muted-foreground";
}

/** One job row. Owns its own apply/poll state so a parent list refresh never
 *  clobbers an application that is mid-apply. */
function JobItem({ job, running }: { job: JobRow; running: boolean }) {
  const app = job.application;
  const [override, setOverride] = useState<StatusState | null>(null);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);

  const state = override ?? app?.state ?? null;

  useEffect(() => {
    if (!app || !state?.polling) {
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
  }, [app, state?.polling]);

  async function onApply(retry: boolean) {
    if (!app) return;
    setOverride({ ...app.state, label: "Starting", polling: true, running: true });
    try {
      setOverride(await (retry ? api.retry(app.id) : api.apply(app.id)));
    } catch (e) {
      setOverride({ ...app.state, label: (e as Error).message, tone: "danger", polling: false });
    }
  }

  const canApply = app?.can_apply && !state?.polling;
  const isFailed = state?.tone === "danger" && !state?.polling;

  return (
    <Card className="gap-0 py-0">
      <CardContent className="flex items-center gap-4 px-4 py-3.5">
        <div
          className={cn(
            "grid size-[52px] shrink-0 place-items-center rounded-md text-[17px] font-semibold",
            scoreClasses(app?.match_score ?? null),
          )}
        >
          {app?.match_score != null ? (
            app.match_score
          ) : running ? (
            <Loader2 className="size-4 animate-spin" />
          ) : (
            "—"
          )}
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
          {app?.score_rationale && (
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
          {!app ? (
            <span className="text-[12.5px] text-muted-foreground">
              {running ? "scoring…" : "not scored"}
            </span>
          ) : canApply ? (
            <Button size="sm" onClick={() => onApply(false)}>
              Apply
            </Button>
          ) : isFailed ? (
            <>
              <Badge variant="destructive">{state?.label}</Badge>
              <Button variant="outline" size="sm" onClick={() => onApply(true)}>
                Retry
              </Button>
            </>
          ) : state?.polling ? (
            <Badge variant="secondary" className="gap-1.5">
              <Loader2 className="size-3 animate-spin" />
              {state.label}
              {state.elapsed ? ` (${state.elapsed}s)` : ""}
            </Badge>
          ) : (
            <Badge variant={toneVariant(state?.tone ?? "neutral")}>
              {state?.label ?? app.status}
            </Badge>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

/** A clickable discovery-source toggle. Enabled = solid; disabled = dashed/muted;
 *  enabled-but-nothing-to-fetch (e.g. an ATS with no companies) = amber warning. */
function SourceChip({
  source,
  busy,
  disabled,
  onToggle,
}: {
  source: SourceInfo;
  busy: boolean;
  disabled: boolean;
  onToggle: () => void;
}) {
  const { name, detail, enabled, ready } = source;
  const notReady = enabled && !ready;
  return (
    <button
      type="button"
      onClick={onToggle}
      disabled={disabled}
      title={
        notReady
          ? "Enabled, but nothing to fetch — add companies/sites in Setup"
          : enabled
            ? "Enabled — click to disable"
            : "Disabled — click to enable"
      }
      className={cn(
        "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1 text-[12.5px] transition-colors disabled:opacity-60",
        enabled
          ? "border-primary/40 bg-primary/10 text-foreground hover:bg-primary/15"
          : "border-dashed text-muted-foreground hover:bg-muted",
      )}
    >
      {busy ? (
        <Loader2 className="size-3 animate-spin" />
      ) : (
        <span
          className={cn(
            "size-1.5 rounded-full",
            notReady ? "bg-amber-500" : enabled ? "bg-success" : "bg-muted-foreground/40",
          )}
        />
      )}
      <span className="font-medium">{name}</span>
      <span className={cn(notReady ? "text-amber-600" : "text-muted-foreground/70")}>
        · {detail}
      </span>
    </button>
  );
}

export default function Jobs() {
  const [busy, setBusy] = useState<"discover" | "rank" | "clear" | null>(null);
  const [toggling, setToggling] = useState<string | null>(null);
  const [filter, setFilter] = useState<"all" | "scored">("all");
  const { data, error, loading, refresh } = usePolling(api.jobs, null);
  const running = data?.running ?? false;

  async function toggleSource(s: SourceInfo) {
    setToggling(s.name);
    try {
      await api.toggleSource(s.name, !s.enabled);
      await refresh();
    } catch (e) {
      alert((e as Error).message);
    } finally {
      setToggling(null);
    }
  }

  // Poll the list while a discover/score run is active.
  const pollTimer = useRef<ReturnType<typeof setInterval> | null>(null);
  useEffect(() => {
    if (running) {
      pollTimer.current = setInterval(refresh, 3000);
      return () => {
        if (pollTimer.current) clearInterval(pollTimer.current);
      };
    }
  }, [running, refresh]);

  async function run(kind: "discover" | "rank" | "clear") {
    if (kind === "clear" && !confirm("Delete ALL jobs and applications?")) return;
    setBusy(kind);
    try {
      if (kind === "discover") await api.discover();
      else if (kind === "rank") await api.rank();
      else await api.clearJobs();
      await refresh();
    } catch (e) {
      alert((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  const jobs = data?.jobs ?? [];
  const sources = data?.sources ?? [];
  const scoredCount = jobs.filter((j) => j.application?.match_score != null).length;
  const visible = filter === "scored"
    ? jobs.filter((j) => j.application?.match_score != null)
    : jobs;

  return (
    <>
      <PageHeader
        title="Jobs"
        subtitle="Discovered postings, ranked by match score — apply to strong matches."
        actions={
          <>
            <Button size="sm" disabled={busy === "discover"} onClick={() => run("discover")}>
              {busy === "discover" ? <Loader2 className="animate-spin" /> : <Search />}
              Discover &amp; score
            </Button>
            <Button
              variant="outline"
              size="sm"
              disabled={busy === "rank"}
              onClick={() => run("rank")}
            >
              {busy === "rank" && <Loader2 className="animate-spin" />}
              Re-score
            </Button>
            <Button
              variant="outline"
              size="sm"
              className="text-destructive hover:bg-destructive/5 hover:text-destructive"
              disabled={busy === "clear"}
              onClick={() => run("clear")}
            >
              {busy === "clear" ? <Loader2 className="animate-spin" /> : <Trash2 />}
              Clear all
            </Button>
          </>
        }
      />

      {!loading && !error && sources.length > 0 && (
        <div className="mb-4">
          <div className="flex flex-wrap items-center gap-1.5">
            <span className="mr-0.5 text-[12.5px] text-muted-foreground">Discovery sources:</span>
            {sources.map((s) => (
              <SourceChip
                key={s.name}
                source={s}
                busy={toggling === s.name}
                disabled={toggling !== null}
                onToggle={() => toggleSource(s)}
              />
            ))}
          </div>
          {!sources.some((s) => s.ready) && (
            <p className="mt-1.5 text-[12px] text-muted-foreground">
              Nothing will be fetched yet — enable a source above (ATS boards also need
              companies added in Setup).
            </p>
          )}
        </div>
      )}

      {running && (
        <Alert className="mb-4">
          <Loader2 className="animate-spin" />
          <AlertDescription>
            Discovering and scoring jobs… new cards appear as they’re ranked.
          </AlertDescription>
        </Alert>
      )}

      {loading ? (
        <div className="flex flex-col gap-2.5">
          {Array.from({ length: 4 }).map((_, i) => (
            <Skeleton key={i} className="h-[84px] rounded-xl" />
          ))}
        </div>
      ) : error ? (
        <Card>
          <CardContent className="py-10 text-center">
            <p className="font-medium">Couldn’t load jobs</p>
            <p className="mt-1 text-sm text-muted-foreground">{error}</p>
          </CardContent>
        </Card>
      ) : jobs.length === 0 ? (
        <Card className="border-dashed">
          <CardContent className="py-14 text-center">
            <p className="font-medium">No jobs yet</p>
            <p className="mt-1.5 text-sm text-muted-foreground">
              Configure sources in Setup, then click <span className="font-medium">Discover &amp; score</span>.
            </p>
          </CardContent>
        </Card>
      ) : (
        <>
          <div className="mb-2.5 flex items-center justify-between gap-3">
            <div className="text-[12.5px] text-muted-foreground">
              {visible.length} {filter === "scored" ? "scored " : ""}
              job{visible.length === 1 ? "" : "s"}
            </div>
            <div className="flex items-center rounded-md border p-0.5 text-[12.5px]">
              {(["all", "scored"] as const).map((f) => (
                <button
                  key={f}
                  type="button"
                  onClick={() => setFilter(f)}
                  className={cn(
                    "rounded px-2.5 py-1 font-medium transition-colors",
                    filter === f
                      ? "bg-accent text-accent-foreground"
                      : "text-muted-foreground hover:text-foreground",
                  )}
                >
                  {f === "all" ? `All (${jobs.length})` : `Scored (${scoredCount})`}
                </button>
              ))}
            </div>
          </div>
          {visible.length === 0 ? (
            <Card className="border-dashed">
              <CardContent className="py-14 text-center">
                <p className="font-medium">No scored jobs yet</p>
                <p className="mt-1.5 text-sm text-muted-foreground">
                  Click <span className="font-medium">Re-score</span> to rank discovered
                  jobs, then strong matches show a score and an Apply button.
                </p>
              </CardContent>
            </Card>
          ) : (
            <div className="flex flex-col gap-2.5">
              {visible.map((job) => (
                <JobItem key={job.id} job={job} running={running} />
              ))}
            </div>
          )}
        </>
      )}
    </>
  );
}
