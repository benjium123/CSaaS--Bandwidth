/**
 * Anchored popovers, measured rather than classed.
 *
 * A right-aligned `absolute` panel is only as good as the assumption that its trigger sits
 * near the right edge: the bell in the top bar does, the one at the FOOT of the rail does
 * not, and the help button moved between the two. Measuring the trigger and clamping the
 * panel to the viewport works in both places and is the same code in both, which is why the
 * bell and the help menu share this rather than each carrying its own right-offset maths.
 *
 * The panel stays a DOM child of its trigger's wrapper (fixed positioning does not change
 * the tree), so the existing outside-click handlers - which test `wrapper.contains(target)` -
 * keep working unchanged.
 */
import * as React from "react";

const GAP = 8;
const EDGE = 8;

/**
 * The one piece of arithmetic here, split out so it can be tested without a DOM:
 * right-align the panel under the anchor, 8px below its bottom edge, then pull it back
 * inside the viewport - never further left than 8px, never closer than 8px to the right.
 */
export function anchoredBox(
  rect: { bottom: number; right: number },
  width: number,
  viewportWidth: number,
): { top: number; left: number } {
  const maxLeft = Math.max(viewportWidth - width - EDGE, EDGE);
  const left = Math.min(Math.max(rect.right - width, EDGE), maxLeft);
  return { top: rect.bottom + GAP, left };
}

/**
 * Fixed-position styles for a panel of `width` anchored to `anchor`, or undefined while
 * closed. Recomputed on resize and on any scroll (capture, so a scrolling inner container
 * counts too), because a fixed panel does not move with its trigger.
 */
export function useAnchoredPosition(
  anchor: React.RefObject<HTMLElement>,
  open: boolean,
  width: number,
): React.CSSProperties | undefined {
  const [style, setStyle] = React.useState<React.CSSProperties | undefined>(undefined);

  React.useEffect(() => {
    if (!open) {
      setStyle(undefined);
      return undefined;
    }

    const update = () => {
      const element = anchor.current;
      if (!element) return;
      const rect = element.getBoundingClientRect();
      const box = anchoredBox(rect, width, window.innerWidth);
      setStyle({ position: "fixed", top: box.top, left: box.left, width });
    };

    update();
    window.addEventListener("resize", update);
    window.addEventListener("scroll", update, true);
    return () => {
      window.removeEventListener("resize", update);
      window.removeEventListener("scroll", update, true);
    };
  }, [anchor, open, width]);

  return style;
}
