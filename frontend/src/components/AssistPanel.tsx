import { useEffect, useRef, useState } from "react";
import { Loader2, X } from "lucide-react";
import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { api, type AssistSnapshot } from "@/lib/api";

// The backend streams a fixed-size viewport screenshot; map client coords onto it.
const VW = 1280;
const VH = 800;

function statusText(s: AssistSnapshot): string {
  switch (s.stage) {
    case "queued":
      return s.queue_pos ? `Queued #${s.queue_pos} — starts when the current one finishes.` : "Starting…";
    case "opening":
      return "Opening & auto-filling…";
    case "live": {
      const base = `Live — ${s.filled ?? 0} field(s) filled`;
      return s.missed?.length ? `${base}; fill manually: ${s.missed.join(", ")}` : base;
    }
    case "done":
      return "Session closed.";
    case "error":
      return s.error ? `Couldn’t start: ${s.error}` : "Couldn’t start — open the posting manually.";
    default:
      return "Connecting…";
  }
}

/** Live co-browse of the managed assist browser for one application. Renders the
 *  streamed JPEG frames and forwards mouse/keyboard input over the websocket so
 *  the user can solve the captcha and click Submit inside the embedded view. */
export default function AssistPanel({
  appId,
  title,
  onClose,
}: {
  appId: number;
  title: string;
  onClose: () => void;
}) {
  const [snap, setSnap] = useState<AssistSnapshot>({ stage: "queued" });
  const [frameUrl, setFrameUrl] = useState<string | null>(null);
  const [startError, setStartError] = useState<string | null>(null);

  const wsRef = useRef<WebSocket | null>(null);
  const imgRef = useRef<HTMLImageElement | null>(null);
  const urlRef = useRef<string | null>(null);
  const lastMove = useRef(0);

  // Enqueue the session + open the stream once, on mount.
  useEffect(() => {
    let closed = false;
    api.assistStart(appId).catch((e) => setStartError((e as Error).message));

    const ws = new WebSocket(api.assistWsUrl(appId));
    ws.binaryType = "arraybuffer";
    wsRef.current = ws;

    ws.onmessage = (ev) => {
      if (!(ev.data instanceof ArrayBuffer)) return;
      const url = URL.createObjectURL(new Blob([ev.data], { type: "image/jpeg" }));
      if (urlRef.current) URL.revokeObjectURL(urlRef.current);
      urlRef.current = url;
      if (!closed) setFrameUrl(url);
    };

    // Poll session status for the stage banner.
    const poll = setInterval(() => {
      api.assistStatus(appId).then((s) => !closed && setSnap(s)).catch(() => {});
    }, 1500);

    return () => {
      closed = true;
      clearInterval(poll);
      try {
        if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "done" }));
        ws.close();
      } catch {
        /* ignore */
      }
      if (urlRef.current) URL.revokeObjectURL(urlRef.current);
    };
  }, [appId]);

  function send(ev: Record<string, unknown>) {
    const ws = wsRef.current;
    if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(ev));
  }

  /** Map a mouse event on the <img> to the streamed viewport's coordinates. */
  function coords(e: React.MouseEvent) {
    const img = imgRef.current;
    if (!img) return { x: 0, y: 0 };
    const r = img.getBoundingClientRect();
    const x = ((e.clientX - r.left) / r.width) * VW;
    const y = ((e.clientY - r.top) / r.height) * VH;
    return { x: Math.max(0, Math.min(VW, x)), y: Math.max(0, Math.min(VH, y)) };
  }

  function onKeyDown(e: React.KeyboardEvent) {
    // Printable single characters go as text; everything else as a named key press.
    if (e.key.length === 1 && !e.ctrlKey && !e.metaKey) {
      send({ type: "text", text: e.key });
    } else if (["Enter", "Backspace", "Tab", "Delete", "Escape", "ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(e.key)) {
      send({ type: "key", key: e.key });
    } else {
      return;
    }
    e.preventDefault();
  }

  const live = snap.stage === "live";

  return (
    <div className="rounded-xl border border-border bg-card">
      <div className="flex items-center justify-between gap-3 border-b border-border px-4 py-3">
        <div className="min-w-0">
          <div className="truncate text-[14px] font-semibold">Co-browse · {title}</div>
          <div className="mt-0.5 flex items-center gap-2 text-[12.5px] text-muted-foreground">
            {(snap.stage === "queued" || snap.stage === "opening") && (
              <Loader2 className="size-3 animate-spin" />
            )}
            <span>{startError ? `Error: ${startError}` : statusText(snap)}</span>
          </div>
        </div>
        <div className="flex items-center gap-2">
          <Badge variant={live ? "default" : snap.stage === "error" ? "destructive" : "secondary"}>
            {snap.stage ?? "…"}
          </Badge>
          <Button variant="outline" size="sm" onClick={onClose}>
            <X /> Close
          </Button>
        </div>
      </div>

      <div
        className="relative bg-muted/40 outline-none"
        tabIndex={0}
        onKeyDown={onKeyDown}
        role="application"
        aria-label="Assisted co-browse view"
      >
        {frameUrl ? (
          <img
            ref={imgRef}
            src={frameUrl}
            alt="Live application form"
            draggable={false}
            className="block w-full select-none"
            style={{ aspectRatio: `${VW} / ${VH}` }}
            onMouseMove={(e) => {
              const now = Date.now();
              if (now - lastMove.current < 40) return; // throttle to ~25/s
              lastMove.current = now;
              send({ type: "move", ...coords(e) });
            }}
            onMouseDown={(e) => send({ type: "down", button: e.button === 2 ? "right" : "left", ...coords(e) })}
            onMouseUp={(e) => send({ type: "up", button: e.button === 2 ? "right" : "left", ...coords(e) })}
            onWheel={(e) => send({ type: "scroll", dx: e.deltaX, dy: e.deltaY })}
            onContextMenu={(e) => e.preventDefault()}
          />
        ) : (
          <div className="flex aspect-[1280/800] items-center justify-center text-sm text-muted-foreground">
            <Loader2 className="mr-2 size-4 animate-spin" />
            Waiting for the live view…
          </div>
        )}
      </div>

      <div className="border-t border-border px-4 py-2.5 text-[12px] text-muted-foreground">
        Click and type inside the frame to solve the captcha and press <b>Submit</b> on the form.
        We never auto-solve captchas or click Submit for you.
      </div>
    </div>
  );
}
