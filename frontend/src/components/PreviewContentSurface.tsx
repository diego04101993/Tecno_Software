import { FileCode2, Globe, ImageIcon, LayoutTemplate, Type, Video } from "lucide-react";

import { isStreamLikeContent, resolvePreviewMediaUrl, type PreviewRenderableEntry } from "../lib/preview";

function resolveUrlPreviewFrame(url: string | null) {
  if (!url) {
    return null;
  }

  const trimmed = url.trim();
  if (!/^https?:\/\//i.test(trimmed)) {
    return null;
  }

  try {
    const parsed = new URL(trimmed);
    const hostname = parsed.hostname.replace(/^www\./i, "").toLowerCase();

    if (hostname === "youtube.com" || hostname === "m.youtube.com") {
      const videoId = parsed.searchParams.get("v");
      if (videoId) {
        return `https://www.youtube.com/embed/${videoId}?rel=0&modestbranding=1`;
      }
    }

    if (hostname === "youtu.be") {
      const videoId = parsed.pathname.replace(/\//g, "");
      if (videoId) {
        return `https://www.youtube.com/embed/${videoId}?rel=0&modestbranding=1`;
      }
    }

    return parsed.toString();
  } catch {
    return null;
  }
}

function getOverlayLabel(entry: PreviewRenderableEntry | null) {
  if (!entry?.content) {
    return null;
  }

  const metadata = entry.content.metadata_json;
  const overlayText = metadata.overlay_text;
  const overlayLabel = metadata.overlay_label;

  if (typeof overlayText === "string" && overlayText.trim()) {
    return overlayText;
  }
  if (typeof overlayLabel === "string" && overlayLabel.trim()) {
    return overlayLabel;
  }

  return null;
}

function PreviewMediaPlaceholder({
  contentType,
  label,
  compact,
}: {
  contentType: "image" | "video";
  label: string;
  compact: boolean;
}) {
  return (
    <div className="grid h-full w-full place-items-center px-6 text-center text-slate-300">
      <div>
        <span className="mx-auto grid h-14 w-14 place-items-center rounded-2xl border border-white/10 bg-white/5">
          {contentType === "video" ? <Video className="h-6 w-6" /> : <ImageIcon className="h-6 w-6" />}
        </span>
        <p className={`mt-4 font-semibold text-white ${compact ? "text-sm" : "text-base"}`}>No hay media visual disponible.</p>
        <p className="mt-2 text-xs text-slate-400">{label}</p>
      </div>
    </div>
  );
}

function PreviewVisualSurface({
  entry,
  title,
  compact,
}: {
  entry: PreviewRenderableEntry;
  title?: string;
  compact: boolean;
}) {
  const content = entry.content;
  if (!content || (content.type !== "image" && content.type !== "video")) {
    return null;
  }

  const overlayLabel = getOverlayLabel(entry);
  const mediaUrl = resolvePreviewMediaUrl(content);
  const baseChrome = compact ? "rounded-[18px]" : "rounded-[24px]";

  return (
    <div className={`relative h-full overflow-hidden border border-slate-200 bg-black shadow-sm ${baseChrome}`}>
      <div className="absolute inset-0 flex items-center justify-center bg-[radial-gradient(circle_at_top,rgba(56,189,248,0.18),transparent_38%),linear-gradient(180deg,#020617_0%,#0f172a_100%)]">
        {mediaUrl ? (
          content.type === "image" ? (
            <img
              alt={title ?? content.name}
              className="h-full w-full object-contain"
              draggable={false}
              loading="eager"
              src={mediaUrl}
            />
          ) : (
            <video
              key={`${entry.id}:${mediaUrl}`}
              autoPlay
              className="h-full w-full object-contain"
              controls={false}
              loop
              muted
              playsInline
              preload="metadata"
              src={mediaUrl}
            />
          )
        ) : (
          <PreviewMediaPlaceholder
            contentType={content.type}
            label={content.file_path ?? content.source_url ?? "Sin URL resuelta"}
            compact={compact}
          />
        )}
      </div>

      {overlayLabel ? (
        <span className="absolute right-4 top-4 z-10 rounded-full border border-white/20 bg-black/45 px-3 py-1 text-[10px] uppercase tracking-[0.2em] text-white/90 backdrop-blur">
          {overlayLabel}
        </span>
      ) : null}

      <div className="pointer-events-none absolute inset-x-0 bottom-0 z-10 bg-gradient-to-t from-black via-black/72 to-transparent px-5 py-4 text-white">
        <div className="flex items-end justify-between gap-4">
          <div className="min-w-0">
            <p className="text-xs uppercase tracking-[0.2em] text-slate-300">{content.type === "video" ? "Video" : "Imagen"}</p>
            <p className={`${compact ? "mt-2 text-base" : "mt-2 text-xl"} truncate font-semibold`} title={title ?? content.name}>
              {title ?? content.name}
            </p>
            <p className="mt-1 truncate text-xs text-slate-300">{entry.contentLabel}</p>
          </div>
          <span className="shrink-0 rounded-full border border-white/10 bg-white/10 px-3 py-1 text-[11px] font-semibold uppercase tracking-[0.16em] text-slate-100">
            {entry.duration_seconds}s
          </span>
        </div>
      </div>
    </div>
  );
}

export function PreviewContentSurface({
  entry,
  title,
  compact = false,
}: {
  entry: PreviewRenderableEntry | null;
  title?: string;
  compact?: boolean;
}) {
  if (!entry) {
    return (
      <div className="flex h-full min-h-40 items-center justify-center rounded-[24px] border border-dashed border-slate-300 bg-slate-100/80 px-5 py-6 text-center text-sm text-slate-500">
        No hay contenido disponible para esta vista previa.
      </div>
    );
  }

  const layout = entry.layout ?? null;

  if (!entry.content && !layout) {
    return (
      <div className="flex h-full min-h-40 items-center justify-center rounded-[24px] border border-dashed border-slate-300 bg-slate-100/80 px-5 py-6 text-center text-sm text-slate-500">
        No hay contenido disponible para esta vista previa.
      </div>
    );
  }

  const overlayLabel = getOverlayLabel(entry);
  const content = entry.content;
  const baseChrome = compact ? "rounded-[18px]" : "rounded-[24px]";

  if (content && (content.type === "image" || content.type === "video")) {
    return <PreviewVisualSurface compact={compact} entry={entry} title={title} />;
  }

  if (content?.type === "url") {
    const sourceUrl = content.source_url ?? "";
    const streamLike = isStreamLikeContent(content);
    const frameUrl = streamLike ? null : resolveUrlPreviewFrame(sourceUrl);

    return (
      <div className={`h-full overflow-hidden border border-slate-200 bg-white shadow-sm ${baseChrome}`}>
        <div className="flex items-center gap-2 border-b border-slate-200 bg-slate-50 px-4 py-3">
          <span className="h-2.5 w-2.5 rounded-full bg-rose-300" />
          <span className="h-2.5 w-2.5 rounded-full bg-amber-300" />
          <span className="h-2.5 w-2.5 rounded-full bg-emerald-300" />
          <div className="ml-2 truncate rounded-full bg-white px-3 py-1 text-xs text-slate-500" title={sourceUrl}>
            {sourceUrl || "URL externa"}
          </div>
        </div>
        <div className="flex h-[calc(100%-57px)] flex-col bg-gradient-to-br from-slate-100 via-white to-cyan-50">
          {frameUrl ? (
            <div className="relative min-h-0 flex-1 overflow-hidden">
              <iframe
                className="h-full w-full bg-white"
                referrerPolicy="strict-origin-when-cross-origin"
                sandbox="allow-same-origin allow-scripts allow-forms allow-popups allow-presentation"
                src={frameUrl}
                title={title ?? content.name}
              />
              <div className="pointer-events-none absolute inset-x-0 bottom-0 bg-gradient-to-t from-slate-950 via-slate-950/80 to-transparent px-5 py-4 text-white">
                <p className="inline-flex items-center gap-2 text-[11px] uppercase tracking-[0.2em] text-cyan-200">
                  <Globe className="h-3.5 w-3.5" />
                  Vista embebida
                </p>
                <p className="mt-2 truncate text-lg font-semibold">{title ?? content.name}</p>
                <p className="mt-1 truncate text-xs text-slate-200">{sourceUrl}</p>
              </div>
            </div>
          ) : (
            <div className="flex h-full flex-col justify-between px-5 py-5">
              <div>
                <p className="inline-flex items-center gap-2 text-xs uppercase tracking-[0.2em] text-cyan-700">
                  <Globe className="h-3.5 w-3.5" />
                  {streamLike ? "Stream o fuente externa" : "Vista web simulada"}
                </p>
                <p className="mt-3 text-lg font-semibold text-ink">{title ?? content.name}</p>
                <p className="mt-2 text-sm text-slate-600">
                  {streamLike
                    ? "El SaaS guarda este enlace para runtime. La reproducción real del stream dependerá del Player."
                    : "Si el sitio no permite iframe, aquí verás una tarjeta representativa con la URL."}
                </p>
              </div>
              <div className="rounded-[18px] border border-slate-200 bg-white/90 px-4 py-4 text-sm text-slate-700">
                <p className="font-semibold text-ink">{sourceUrl || "URL pendiente"}</p>
              </div>
            </div>
          )}
        </div>
      </div>
    );
  }

  if (content?.type === "html") {
    return (
      <div className={`h-full overflow-hidden border border-slate-200 bg-white shadow-sm ${baseChrome}`}>
        <div className="border-b border-slate-200 bg-slate-50 px-4 py-3">
          <p className="inline-flex items-center gap-2 text-xs uppercase tracking-[0.2em] text-slate-500">
            <FileCode2 className="h-3.5 w-3.5" />
            HTML basico
          </p>
        </div>
        <div className="prose prose-sm max-w-none overflow-auto px-5 py-5 text-slate-700">
          <div dangerouslySetInnerHTML={{ __html: content.html_content ?? "<p>Sin HTML disponible.</p>" }} />
        </div>
      </div>
    );
  }

  if (content?.type === "text") {
    return (
      <div className={`relative h-full overflow-hidden border border-slate-200 bg-gradient-to-br from-slate-900 via-slate-800 to-cyan-900 p-6 text-white shadow-sm ${baseChrome}`}>
        {overlayLabel ? (
          <span className="absolute right-4 top-4 rounded-full border border-white/20 bg-white/10 px-3 py-1 text-[10px] uppercase tracking-[0.2em] text-white/90">
            {overlayLabel}
          </span>
        ) : null}
        <div className="flex h-full flex-col justify-between">
          <div>
            <p className="inline-flex items-center gap-2 text-xs uppercase tracking-[0.2em] text-cyan-200">
              <Type className="h-3.5 w-3.5" />
              Texto dinamico
            </p>
            <p className={`${compact ? "mt-3 text-lg" : "mt-4 text-3xl"} font-semibold leading-tight`}>{content.text_content ?? content.name}</p>
          </div>
          <p className="text-sm text-cyan-100/80">{title ?? content.name}</p>
        </div>
      </div>
    );
  }

  return (
    <div className={`relative h-full overflow-hidden border border-slate-200 bg-gradient-to-br from-slate-950 via-slate-800 to-slate-700 text-white shadow-sm ${baseChrome}`}>
      {overlayLabel ? (
        <span className="absolute right-4 top-4 z-10 rounded-full border border-white/20 bg-white/10 px-3 py-1 text-[10px] uppercase tracking-[0.2em] text-white/90">
          {overlayLabel}
        </span>
      ) : null}
      <div className="absolute inset-0 bg-[radial-gradient(circle_at_top,rgba(255,255,255,0.18),transparent_40%)]" />
      <div className="relative flex h-full flex-col justify-between px-5 py-5">
        <div>
          <p className="inline-flex items-center gap-2 text-xs uppercase tracking-[0.2em] text-slate-300">
            <LayoutTemplate className="h-3.5 w-3.5" />
            Layout
          </p>
          <p className={`${compact ? "mt-2 text-lg" : "mt-3 text-2xl"} font-semibold`}>{title ?? layout?.name ?? content?.name ?? "Layout"}</p>
          <p className="mt-2 text-sm text-slate-300">{entry.contentLabel}</p>
        </div>
        <div className="rounded-[18px] border border-white/10 bg-black/20 px-4 py-4 text-sm text-slate-200">
          {layout ? `${layout.canvas_width}x${layout.canvas_height} · ${layout.template}` : content?.file_path ?? "Archivo visual listo para reproducirse en pantalla"}
        </div>
      </div>
    </div>
  );
}
