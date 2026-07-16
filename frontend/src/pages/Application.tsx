import { useEffect, useRef, useState } from "react";
import { Download, FileText, Loader2, Send, Wand2 } from "lucide-react";
import { toast } from "sonner";
import { PageHeader } from "@/components/Layout";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Card, CardContent } from "@/components/ui/card";
import { Skeleton } from "@/components/ui/skeleton";
import { api, type ApplicationRow, type StatusState } from "@/lib/api";
import { usePolling } from "@/lib/hooks";
import { toneVariant } from "@/lib/tone";

function Row({ row }: { row: ApplicationRow }) {
  const [override, setOverride] = useState<StatusState | null>(null);
  const timer = useRef<ReturnType<typeof setInterval> | null>(null);
  const state = override ?? row.state;

  useEffect(() => {
    if (!state.polling) {
      if (timer.current) clearInterval(timer.current);
      return;
    }
    timer.current = setInterval(async () => {
      try {
        const next = await api.applyStatus(row.id);
        setOverride(next);
        if (!next.polling && timer.current) clearInterval(timer.current);
      } catch {
        /* keep last known state */
      }
    }, 2000);
    return () => {
      if (timer.current) clearInterval(timer.current);
    };
  }, [row.id, state.polling]);

  async function onRetry() {
    setOverride({ ...row.state, label: "Starting", polling: true, running: true });
    try {
      setOverride(await api.retry(row.id));
    } catch (e) {
      setOverride({ ...row.state, label: (e as Error).message, tone: "danger", polling: false });
    }
  }

  return (
    <Card className="gap-0 py-0">
      <CardContent className="flex items-center gap-4 px-4 py-3">
        <div className="w-9 shrink-0 text-center text-[15px] font-semibold text-muted-foreground">
          {row.match_score ?? "—"}
        </div>

        <div className="min-w-0 flex-1">
          <div className="text-[14px] font-semibold">{row.title}</div>
          <div className="mt-0.5 flex flex-wrap items-center gap-2 text-[12.5px] text-muted-foreground">
            <span>{row.company}</span>
            <span className="text-border">·</span>
            <Badge variant="outline" className="font-normal">
              {row.source}
            </Badge>
          </div>
          {row.error && <p className="mt-1 text-[12.5px] text-destructive">{row.error}</p>}
        </div>

        {/* Artifacts */}
        <div className="flex shrink-0 items-center gap-1.5">
          {row.has_resume && (
            <Button asChild variant="ghost" size="sm" title="Download tailored résumé">
              <a href={api.artifactUrl(row.id, "resume")} target="_blank" rel="noreferrer">
                <FileText className="size-3.5" /> CV
              </a>
            </Button>
          )}
          {row.has_cover_letter && (
            <Button asChild variant="ghost" size="sm" title="Download cover letter">
              <a href={api.artifactUrl(row.id, "cover_letter")} target="_blank" rel="noreferrer">
                <Download className="size-3.5" /> Letter
              </a>
            </Button>
          )}
        </div>

        {/* Status / retry */}
        <div className="flex w-[150px] shrink-0 justify-end">
          {state.polling ? (
            <Badge variant="secondary" className="gap-1.5">
              <Loader2 className="size-3 animate-spin" />
              {state.label}
              {state.elapsed ? ` (${state.elapsed}s)` : ""}
            </Badge>
          ) : row.can_retry ? (
            <div className="flex items-center gap-2">
              <Badge variant={toneVariant(state.tone)}>{state.label}</Badge>
              <Button variant="outline" size="sm" onClick={onRetry}>
                Retry
              </Button>
            </div>
          ) : (
            <Badge variant={toneVariant(state.tone)}>{state.label}</Badge>
          )}
        </div>
      </CardContent>
    </Card>
  );
}

export default function Applications() {
  const { data, error, loading, refresh } = usePolling(api.applications, null);
  const [busy, setBusy] = useState<"tailor" | "submit" | null>(null);

  async function run(kind: "tailor" | "submit") {
    setBusy(kind);
    try {
      if (kind === "tailor") {
        const res = await api.tailor();
        toast.success(`Tailored ${res.tailored ?? 0} application(s)`);
      } else {
        const res = await api.submit();
        toast.success(`Submitted ${res.submitted ?? 0} application(s)`);
      }
      await refresh();
    } catch (e) {
      toast.error((e as Error).message);
    } finally {
      setBusy(null);
    }
  }

  const rows = data?.applications ?? [];

  return (
    <>
      <PageHeader
        title="Applications"
        subtitle="Every application, its status, and generated résumé / cover letter."
        actions={
          <>
            <Button size="sm" variant="outline" disabled={busy === "tailor"} onClick={() => run("tailor")}>
              {busy === "tailor" ? <Loader2 className="animate-spin" /> : <Wand2 />}
              Tailor above threshold
            </Button>
            <Button size="sm" disabled={busy === "submit"} onClick={() => run("submit")}>
              {busy === "submit" ? <Loader2 className="animate-spin" /> : <Send />}
              Submit tailored
            </Button>
          </>
        }
      />

      {loading ? (
        <div className="flex flex-col gap-2">
          {Array.from({ length: 4 }).map((_, i) => (
            <Skeleton key={i} className="h-[68px] rounded-xl" />
          ))}
        </div>
      ) : error ? (
        <Card>
          <CardContent className="py-10 text-center">
            <p className="font-medium">Couldn’t load applications</p>
            <p className="mt-1 text-sm text-muted-foreground">{error}</p>
          </CardContent>
        </Card>
      ) : rows.length === 0 ? (
        <Card className="border-dashed">
          <CardContent className="py-14 text-center">
            <p className="font-medium">No applications yet</p>
            <p className="mt-1.5 text-sm text-muted-foreground">
              Rank jobs in Matches, then tailor and submit them here.
            </p>
          </CardContent>
        </Card>
      ) : (
        <div className="flex flex-col gap-2">
          {rows.map((row) => (
            <Row key={row.id} row={row} />
          ))}
        </div>
      )}
    </>
  );
}
