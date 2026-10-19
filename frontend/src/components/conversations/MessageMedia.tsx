import { useCallback, useEffect, useRef, useState } from "react";
import { createPortal } from "react-dom";
import {
  ChevronLeft,
  ChevronRight,
  Contact,
  Download,
  FileText,
  Music,
  Paperclip,
  Play,
  X,
} from "lucide-react";
import { cn } from "@/lib/utils";

export interface MediaItem {
  url: string;
  content_type?: string | null;
  size_bytes?: number | null;
}

export type MediaKind = "image" | "video" | "audio" | "pdf" | "contact" | "file";

type TileKind = Exclude<MediaKind, "image" | "video">;

const EXTENSION_KINDS: Record<string, MediaKind> = {
  jpg: "image",
  jpeg: "image",
  png: "image",
  gif: "image",
  webp: "image",
  avif: "image",
  bmp: "image",
  heic: "image",
  heif: "image",
  mp4: "video",
  m4v: "video",
  mov: "video",
  webm: "video",
  "3gp": "video",
  "3gpp": "video",
  mp3: "audio",
  m4a: "audio",
  wav: "audio",
  ogg: "audio",
  oga: "audio",
  opus: "audio",
  aac: "audio",
  pdf: "pdf",
  vcf: "contact",
  vcard: "contact",
};

const OPEN_LABELS: Record<MediaKind, string> = {
  image: "Open photo",
  video: "Play video",
  audio: "Play audio",
  pdf: "Open PDF",
  contact: "Open contact card",
  file: "Open file",
};

const TILE_META: Record<TileKind, { label: string; Icon: typeof FileText }> = {
  pdf: { label: "PDF", Icon: FileText },
  audio: { label: "Audio", Icon: Music },
  contact: { label: "Contact card", Icon: Contact },
  file: { label: "Attachment", Icon: Paperclip },
};

function kindFromContentType(contentType: string): MediaKind | null {
  const value = contentType.split(";")[0].trim().toLowerCase();
  if (!value) return null;
  if (value === "application/pdf") return "pdf";
  if (value === "text/vcard" || value === "text/x-vcard") return "contact";
  if (value.startsWith("image/")) return "image";
  if (value.startsWith("video/")) return "video";
  if (value.startsWith("audio/")) return "audio";
  return null;
}

function urlPath(url: string): string {
  return url.split("?")[0].split("#")[0];
}

function lastPathSegment(url: string): string {
  const path = urlPath(url);
  return path.slice(path.lastIndexOf("/") + 1);
}

function kindFromUrl(url: string): MediaKind | null {
  const segment = lastPathSegment(url);
  const dot = segment.lastIndexOf(".");
  if (dot < 0) return null;
  return EXTENSION_KINDS[segment.slice(dot + 1).toLowerCase()] ?? null;
}

export function mediaKind(item: MediaItem): MediaKind {
  const fromType = item.content_type ? kindFromContentType(item.content_type) : null;
  if (fromType) return fromType;
  return kindFromUrl(item.url) ?? "file";
}

function attachmentName(url: string): string {
  const segment = lastPathSegment(url);
  if (!segment) return "";
  try {
    return decodeURIComponent(segment);
  } catch {
    return segment;
  }
}

function formatSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "";
  if (bytes < 1000) return `${Math.round(bytes)} B`;
  if (bytes < 1_000_000) return `${Math.round(bytes / 1000)} KB`;
  if (bytes < 1_000_000_000) return `${(bytes / 1_000_000).toFixed(1)} MB`;
  return `${(bytes / 1_000_000_000).toFixed(1)} GB`;
}

function sizeLabel(item: MediaItem): string {
  return typeof item.size_bytes === "number" ? formatSize(item.size_bytes) : "";
}

interface ThumbnailProps {
  item: MediaItem;
  outbound: boolean;
  onOpen: () => void;
  registerRef: (element: HTMLButtonElement | null) => void;
}

function Thumbnail({ item, outbound, onOpen, registerRef }: ThumbnailProps) {
  const [imageFailed, setImageFailed] = useState(false);
  const detected = mediaKind(item);
  // A broken photo falls back to the generic attachment tile instead of a broken-image icon.
  const kind: MediaKind = detected === "image" && imageFailed ? "file" : detected;

  const surface = outbound
    ? "bg-white/15 text-white"
    : "bg-black/5 text-neutral-700 dark:bg-white/10 dark:text-neutral-100";

  const sizeClass =
    kind === "image" ? "h-40 max-w-[240px]" : kind === "pdf" ? "h-28 w-40" : "h-40 w-40";

  let content: JSX.Element;
  if (kind === "image") {
    content = (
      <img
        src={item.url}
        alt="Photo attachment"
        loading="lazy"
        className="h-40 w-auto max-w-[240px] object-cover"
        onError={() => setImageFailed(true)}
      />
    );
  } else if (kind === "video") {
    content = (
      <>
        <video
          src={item.url}
          preload="metadata"
          muted
          playsInline
          className="h-full w-full object-cover"
        />
        <span className="pointer-events-none absolute inset-0 flex items-center justify-center">
          <span className="flex h-10 w-10 items-center justify-center rounded-full bg-black/60 text-white">
            <Play className="h-5 w-5" aria-hidden="true" />
          </span>
        </span>
      </>
    );
  } else {
    const { label, Icon } = TILE_META[kind];
    const name = kind === "pdf" ? attachmentName(item.url) || "Document" : "";
    const size = sizeLabel(item);
    content = (
      <>
        <Icon className="h-5 w-5" aria-hidden="true" />
        <span className="mt-1 px-1 text-[11px] font-medium leading-tight">{label}</span>
        {name ? (
          <span className="max-w-full truncate px-1 text-[10px] opacity-80">{name}</span>
        ) : null}
        {size ? <span className="text-[10px] opacity-70">{size}</span> : null}
      </>
    );
  }

  return (
    <button
      type="button"
      ref={registerRef}
      onClick={onOpen}
      aria-label={OPEN_LABELS[kind]}
      className={cn(
        "relative flex shrink-0 flex-col items-center justify-center overflow-hidden rounded-xl outline-none transition-colors focus-visible:ring-2 focus-visible:ring-sky-500 focus-visible:ring-offset-2",
        sizeClass,
        kind === "image" ? "" : surface,
      )}
    >
      {content}
    </button>
  );
}

function FilePreview({ item, kind }: { item: MediaItem; kind: "contact" | "file" }) {
  const { label, Icon } = TILE_META[kind];
  const name = attachmentName(item.url) || label;
  const size = sizeLabel(item);

  return (
    <div className="flex w-[min(92vw,420px)] flex-col items-center gap-2 rounded-xl bg-white p-6 text-center text-neutral-900 shadow-xl">
      <Icon className="h-8 w-8 text-neutral-500" aria-hidden="true" />
      <p className="text-sm font-medium">{label}</p>
      <p className="w-full truncate text-xs text-neutral-500">{name}</p>
      {size ? <p className="text-xs text-neutral-400">{size}</p> : null}
      <a
        href={item.url}
        download
        target="_blank"
        rel="noopener noreferrer"
        aria-label="Download file"
        className="mt-2 inline-flex items-center gap-1.5 rounded-lg bg-neutral-900 px-3 py-1.5 text-xs font-medium text-white hover:bg-neutral-800"
      >
        <Download className="h-4 w-4" aria-hidden="true" />
        Download
      </a>
    </div>
  );
}

interface PdfPreviewProps {
  url: string;
}

function PdfPreview({ url }: PdfPreviewProps) {
  const [state, setState] = useState<
    { status: "loading" } | { status: "ready"; src: string } | { status: "error" }
  >({ status: "loading" });

  useEffect(() => {
    const controller = new AbortController();
    let active = true;
    let objectUrl: string | null = null;
    setState({ status: "loading" });

    // The signed URL refuses to render in an iframe, so fetch the bytes and use a blob URL.
    void (async () => {
      try {
        const response = await fetch(url, { signal: controller.signal });
        if (!response.ok) throw new Error(`PDF request failed: ${response.status}`);
        const buffer = await response.arrayBuffer();
        if (!active) return;
        objectUrl = URL.createObjectURL(new Blob([buffer], { type: "application/pdf" }));
        setState({ status: "ready", src: objectUrl });
      } catch (error) {
        if (!active) return;
        if (error instanceof DOMException && error.name === "AbortError") return;
        setState({ status: "error" });
      }
    })();

    return () => {
      active = false;
      controller.abort();
      if (objectUrl) URL.revokeObjectURL(objectUrl);
    };
  }, [url]);

  if (state.status === "loading") {
    return (
      <div className="flex h-[90vh] w-[min(92vw,1000px)] items-center justify-center rounded bg-white text-sm text-neutral-500">
        Loading PDF…
      </div>
    );
  }

  if (state.status === "error") {
    return (
      <div className="flex flex-col items-center gap-3 rounded bg-white p-6 text-center text-neutral-900">
        <p className="text-sm">{"Couldn't open this PDF"}</p>
        <a
          href={url}
          download
          target="_blank"
          rel="noopener noreferrer"
          aria-label="Download file"
          className="inline-flex items-center gap-1.5 rounded-lg bg-neutral-900 px-3 py-1.5 text-xs font-medium text-white hover:bg-neutral-800"
        >
          <Download className="h-4 w-4" aria-hidden="true" />
          Download
        </a>
      </div>
    );
  }

  return (
    <iframe
      title="PDF attachment"
      src={state.src}
      className="h-[90vh] w-[min(92vw,1000px)] rounded bg-white"
    />
  );
}

function ViewerContent({ item }: { item: MediaItem }) {
  const kind = mediaKind(item);

  switch (kind) {
    case "image":
      return (
        <img
          src={item.url}
          alt="Photo attachment"
          className="max-h-[90vh] max-w-[92vw] object-contain"
        />
      );
    case "video":
      return (
        <video
          src={item.url}
          controls
          autoPlay
          playsInline
          className="max-h-[90vh] max-w-[92vw]"
        />
      );
    case "audio":
      return (
        <div className="rounded-xl bg-white p-4 shadow-xl">
          <audio src={item.url} controls autoPlay />
        </div>
      );
    case "pdf":
      return <PdfPreview url={item.url} />;
    default:
      return <FilePreview item={item} kind={kind} />;
  }
}

interface MediaViewerProps {
  items: MediaItem[];
  index: number;
  onClose: () => void;
  onNavigate: (index: number) => void;
}

function MediaViewer({ items, index, onClose, onNavigate }: MediaViewerProps) {
  const closeRef = useRef<HTMLButtonElement | null>(null);
  const hasMany = items.length > 1;
  const item = items[index];

  useEffect(() => {
    closeRef.current?.focus();
  }, []);

  useEffect(() => {
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      document.body.style.overflow = previous;
    };
  }, []);

  useEffect(() => {
    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        event.preventDefault();
        onClose();
        return;
      }
      if (!hasMany) return;
      if (event.key === "ArrowLeft") {
        event.preventDefault();
        onNavigate((index - 1 + items.length) % items.length);
      } else if (event.key === "ArrowRight") {
        event.preventDefault();
        onNavigate((index + 1) % items.length);
      }
    }

    document.addEventListener("keydown", handleKeyDown);
    return () => document.removeEventListener("keydown", handleKeyDown);
  }, [hasMany, index, items.length, onClose, onNavigate]);

  if (!item) return null;

  return createPortal(
    <div
      role="dialog"
      aria-modal="true"
      aria-label="Attachment viewer"
      className="fixed inset-0 z-[1000] flex items-center justify-center bg-black/85 p-4"
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        className="absolute right-2 top-2 flex items-center gap-1"
        onClick={(event) => event.stopPropagation()}
      >
        <a
          href={item.url}
          download
          target="_blank"
          rel="noopener noreferrer"
          aria-label="Download"
          className="flex h-11 w-11 items-center justify-center rounded-full text-white/90 hover:bg-white/10 hover:text-white focus:outline-none focus-visible:ring-2 focus-visible:ring-white"
        >
          <Download className="h-5 w-5" aria-hidden="true" />
        </a>
        <button
          ref={closeRef}
          type="button"
          aria-label="Close"
          onClick={onClose}
          className="flex h-11 w-11 items-center justify-center rounded-full text-white/90 hover:bg-white/10 hover:text-white focus:outline-none focus-visible:ring-2 focus-visible:ring-white"
        >
          <X className="h-6 w-6" aria-hidden="true" />
        </button>
      </div>

      {hasMany ? (
        <button
          type="button"
          aria-label="Previous"
          onClick={() => onNavigate((index - 1 + items.length) % items.length)}
          className="absolute left-2 top-1/2 flex h-11 w-11 -translate-y-1/2 items-center justify-center rounded-full bg-black/40 text-white hover:bg-black/60 focus:outline-none focus-visible:ring-2 focus-visible:ring-white"
        >
          <ChevronLeft className="h-7 w-7" aria-hidden="true" />
        </button>
      ) : null}

      {hasMany ? (
        <button
          type="button"
          aria-label="Next"
          onClick={() => onNavigate((index + 1) % items.length)}
          className="absolute right-2 top-1/2 flex h-11 w-11 -translate-y-1/2 items-center justify-center rounded-full bg-black/40 text-white hover:bg-black/60 focus:outline-none focus-visible:ring-2 focus-visible:ring-white"
        >
          <ChevronRight className="h-7 w-7" aria-hidden="true" />
        </button>
      ) : null}

      <div
        className="relative flex flex-col items-center gap-3"
        onClick={(event) => event.stopPropagation()}
      >
        <ViewerContent item={item} />
      </div>

      {hasMany ? (
        <div className="pointer-events-none absolute bottom-3 left-1/2 -translate-x-1/2 rounded-full bg-black/60 px-3 py-1 text-xs font-medium text-white">
          {`${index + 1} / ${items.length}`}
        </div>
      ) : null}
    </div>,
    document.body,
  );
}

export function MessageMedia({
  items,
  outbound = false,
}: {
  items: MediaItem[];
  outbound?: boolean;
}): JSX.Element | null {
  const [openIndex, setOpenIndex] = useState<number | null>(null);
  const openIndexRef = useRef<number | null>(null);
  const thumbRefs = useRef<Array<HTMLButtonElement | null>>([]);

  const openAt = useCallback((index: number) => {
    openIndexRef.current = index;
    setOpenIndex(index);
  }, []);

  const closeViewer = useCallback(() => {
    const index = openIndexRef.current;
    openIndexRef.current = null;
    setOpenIndex(null);
    // Restore focus to the thumbnail that opened the viewer.
    if (index !== null) thumbRefs.current[index]?.focus();
  }, []);

  if (items.length === 0) return null;

  return (
    <>
      <div className="flex flex-wrap gap-1.5">
        {items.map((item, index) => (
          <Thumbnail
            key={`${item.url}-${index}`}
            item={item}
            outbound={outbound}
            onOpen={() => openAt(index)}
            registerRef={(element) => {
              thumbRefs.current[index] = element;
            }}
          />
        ))}
      </div>
      {openIndex !== null ? (
        <MediaViewer
          items={items}
          index={openIndex}
          onClose={closeViewer}
          onNavigate={openAt}
        />
      ) : null}
    </>
  );
}
