// PDF preview of a compiled version with PDF.js (no script evaluation, no text layer).
// Positions exchanged with SyncTeX are PDF points from the page's top-left corner.
import {
  GlobalWorkerOptions,
  getDocument,
  type PDFDocumentLoadingTask,
  type PDFDocumentProxy,
  type PageViewport,
} from "pdfjs-dist";
import workerUrl from "pdfjs-dist/build/pdf.worker.min.mjs?url";
import { useEffect, useRef, useState } from "react";
import { API } from "../api";

GlobalWorkerOptions.workerSrc = workerUrl;

const MIN_SCALE = 0.5;
const MAX_SCALE = 2;

export interface Highlight {
  page: number;
  rects: { x: number; y: number; width: number; height: number }[];
}

interface PageInfo {
  number: number;
  viewport: PageViewport;
  height: number; // page height in points
  originX: number;
}

interface Props {
  versionId: string;
  highlight: Highlight | null;
  onInverse: (page: number, x: number, y: number) => void;
}

export function PdfView({ versionId, highlight, onInverse }: Props) {
  const [pages, setPages] = useState<PageInfo[]>([]);
  const [error, setError] = useState<string | null>(null);
  const doc = useRef<PDFDocumentProxy | null>(null);
  const task = useRef<PDFDocumentLoadingTask | null>(null);
  const canvases = useRef(new Map<number, HTMLCanvasElement>());
  const containers = useRef(new Map<number, HTMLDivElement>());
  const root = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let cancelled = false;
    setPages([]);
    setError(null);
    (async () => {
      const reply = await fetch(`${API}/versions/${encodeURIComponent(versionId)}/content`, {
        credentials: "same-origin",
        cache: "no-store",
      });
      if (!reply.ok) throw new Error(`the PDF could not be loaded (HTTP ${reply.status})`);
      const data = new Uint8Array(await reply.arrayBuffer());
      // PDF.js 6 evaluates no generated code; XFA forms stay off.
      const loading = getDocument({ data, enableXfa: false });
      task.current = loading;
      const loaded = await loading.promise;
      if (cancelled) return;
      doc.current = loaded;
      const infos: PageInfo[] = [];
      for (let n = 1; n <= Math.min(loaded.numPages, 200); n += 1) {
        const page = await loaded.getPage(n);
        const [x0, y0, x1, y1] = page.view;
        // Fit the page to the pane's width (no horizontal scrolling).
        const available = Math.max((root.current?.clientWidth ?? 600) - 8, 200);
        const fit = available / Math.max((x1 ?? 612) - (x0 ?? 0), 1);
        infos.push({
          number: n,
          viewport: page.getViewport({ scale: Math.min(MAX_SCALE, Math.max(MIN_SCALE, fit)) }),
          height: (y1 ?? 0) - (y0 ?? 0),
          originX: x0 ?? 0,
        });
      }
      if (!cancelled) setPages(infos);
    })().catch((exc: unknown) => {
      if (!cancelled) setError(exc instanceof Error ? exc.message : String(exc));
    });
    return () => {
      cancelled = true;
      void task.current?.destroy();
      task.current = null;
      doc.current = null;
    };
  }, [versionId]);

  useEffect(() => {
    const loaded = doc.current;
    if (!loaded || pages.length === 0) return;
    let cancelled = false;
    (async () => {
      for (const info of pages) {
        if (cancelled) return;
        const canvas = canvases.current.get(info.number);
        if (!canvas) continue;
        const page = await loaded.getPage(info.number);
        canvas.width = Math.floor(info.viewport.width);
        canvas.height = Math.floor(info.viewport.height);
        await page.render({ canvas, viewport: info.viewport }).promise;
        canvas.dataset.rendered = "true";
      }
    })().catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [pages]);

  useEffect(() => {
    if (!highlight) return;
    containers.current.get(highlight.page)?.scrollIntoView({ block: "center" });
  }, [highlight]);

  return (
    <div className="tex-pdf-pages" data-testid="pdf-pages" ref={root}>
      {error ? (
        <p className="state state-error" role="alert">
          {error}
        </p>
      ) : null}
      {!error && pages.length === 0 ? (
        <p className="state state-loading" role="status">
          <span className="spinner" aria-hidden="true" /> Loading PDF…
        </p>
      ) : null}
      {pages.map((info) => {
        const rects =
          highlight && highlight.page === info.number
            ? highlight.rects.map((r) => {
                const [ax, ay] = info.viewport.convertToViewportPoint(
                  r.x + info.originX,
                  info.height - r.y - r.height,
                ) as [number, number];
                const [bx, by] = info.viewport.convertToViewportPoint(
                  r.x + info.originX + r.width,
                  info.height - r.y,
                ) as [number, number];
                return {
                  left: Math.min(ax, bx),
                  top: Math.min(ay, by),
                  width: Math.abs(bx - ax),
                  height: Math.abs(by - ay),
                };
              })
            : [];
        return (
          <div
            key={info.number}
            className="tex-pdf-page"
            data-testid="pdf-page"
            data-page={info.number}
            ref={(el) => {
              if (el) containers.current.set(info.number, el);
            }}
            role="group"
            aria-label={`PDF page ${info.number}. Clicking opens the matching source line.`}
            onClick={(event) => {
              const canvas = canvases.current.get(info.number);
              if (!canvas) return;
              const box = canvas.getBoundingClientRect();
              const cssX = ((event.clientX - box.left) * info.viewport.width) / box.width;
              const cssY = ((event.clientY - box.top) * info.viewport.height) / box.height;
              const [px, py] = info.viewport.convertToPdfPoint(cssX, cssY) as [number, number];
              onInverse(info.number, px - info.originX, info.height - py);
            }}
          >
            <canvas
              ref={(el) => {
                if (el) canvases.current.set(info.number, el);
              }}
              aria-hidden="true"
            />
            {rects.map((r, i) => (
              <span
                key={i}
                className="tex-pdf-highlight"
                data-testid="pdf-highlight"
                style={{ left: r.left, top: r.top, width: r.width, height: r.height }}
              />
            ))}
          </div>
        );
      })}
    </div>
  );
}
