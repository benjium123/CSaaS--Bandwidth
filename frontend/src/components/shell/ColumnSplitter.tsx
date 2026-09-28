/**
 * The drag handle between two inbox columns.
 *
 * The separator itself is a ZERO-WIDTH flex item, so it adds nothing to the layout; the
 * thing you actually grab is an 8px invisible hit area straddling that line. Everything a
 * pointer can do, the keyboard can do too: arrows move the column by 16px and Enter toggles
 * it. Dragging far past the minimum snaps the column shut, which is the gesture people
 * reach for instead of hunting the button.
 */
import * as React from "react";
import { ChevronLeft, ChevronRight } from "lucide-react";
import { cn } from "@/lib/utils";
import { COLUMN_LIMITS, clampWidth, type ColumnKey } from "@/components/shell/useColumnWidths";

const KEYBOARD_STEP = 16;
const SNAP_PAST_MIN = 60;

export function ColumnSplitter(props: {
  column: ColumnKey;
  label: string;
  width: number;
  closed: boolean;
  onResize: (px: number) => void;
  onToggle: () => void;
  onSnapClosed?: () => void;
}): JSX.Element {
  const { column, label, width, closed, onResize, onToggle, onSnapClosed } = props;
  const limits = COLUMN_LIMITS[column];
  const [dragging, setDragging] = React.useState(false);
  const startXRef = React.useRef(0);
  const startWidthRef = React.useRef(0);

  const onPointerDown = (event: React.PointerEvent<HTMLDivElement>) => {
    if (closed) return;
    startXRef.current = event.clientX;
    startWidthRef.current = width;
    setDragging(true);
    try {
      event.currentTarget.setPointerCapture(event.pointerId);
    } catch {
      // jsdom and older browsers do not implement pointer capture; drag still works without it.
    }
  };

  const onPointerMove = (event: React.PointerEvent<HTMLDivElement>) => {
    if (!dragging) return;
    onResize(clampWidth(column, startWidthRef.current + (event.clientX - startXRef.current)));
  };

  const onPointerUp = (event: React.PointerEvent<HTMLDivElement>) => {
    if (!dragging) return;
    setDragging(false);
    try {
      event.currentTarget.releasePointerCapture(event.pointerId);
    } catch {
      // see setPointerCapture above
    }

    const raw = startWidthRef.current + (event.clientX - startXRef.current);
    if (raw < limits.min - SNAP_PAST_MIN) {
      if (onSnapClosed) onSnapClosed();
      else onToggle();
    }
  };

  const onKeyDown = (event: React.KeyboardEvent<HTMLDivElement>) => {
    if (event.key === "ArrowLeft") {
      event.preventDefault();
      onResize(clampWidth(column, width - KEYBOARD_STEP));
    } else if (event.key === "ArrowRight") {
      event.preventDefault();
      onResize(clampWidth(column, width + KEYBOARD_STEP));
    } else if (event.key === "Enter") {
      event.preventDefault();
      onToggle();
    }
  };

  return (
    <div
      role="separator"
      aria-orientation="vertical"
      aria-label={label}
      aria-valuenow={width}
      aria-valuemin={limits.min}
      aria-valuemax={limits.max}
      tabIndex={0}
      onDoubleClick={onToggle}
      onKeyDown={onKeyDown}
      className="group relative z-10 w-0 shrink-0"
    >
      {closed ? null : (
        <div
          className="absolute inset-y-0 -left-1 w-2 cursor-col-resize"
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
        />
      )}

      <span
        aria-hidden="true"
        className={cn(
          "absolute inset-y-0 -left-px w-0.5 transition-colors",
          dragging ? "bg-primary" : "bg-border group-hover:bg-primary",
        )}
      />

      <button
        type="button"
        aria-label={closed ? `Open ${label}` : `Close ${label}`}
        onClick={onToggle}
        className={cn(
          "absolute left-1/2 top-1/2 z-20 flex h-[22px] w-[22px] -translate-x-1/2 -translate-y-1/2 items-center justify-center rounded-full border border-border bg-background text-muted-foreground shadow-sm hover:bg-muted",
          closed
            ? "opacity-100"
            : "opacity-0 transition-opacity focus-visible:opacity-100 group-hover:opacity-100 group-focus-within:opacity-100",
        )}
      >
        {closed ? (
          <ChevronRight className="h-3.5 w-3.5" aria-hidden="true" />
        ) : (
          <ChevronLeft className="h-3.5 w-3.5" aria-hidden="true" />
        )}
      </button>
    </div>
  );
}
