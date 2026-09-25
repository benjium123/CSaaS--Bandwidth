import { ArrowLeft } from "lucide-react";

/**
 * List/detail pages show one pane at a time below `md` (the inbox's pattern): the list,
 * or the open item with this button to get back to it. Hidden from `md` up, where both
 * panes sit side by side.
 */
export function MobileBack({ label, onBack }: { label: string; onBack: () => void }) {
  return (
    <button
      type="button"
      onClick={onBack}
      className="mx-[18px] mt-[14px] inline-flex h-9 items-center gap-2 rounded-full border border-[hsl(var(--cx-line))] px-[14px] text-[13px] font-medium text-[hsl(var(--cx-text))] md:hidden"
    >
      <ArrowLeft className="h-4 w-4" aria-hidden="true" />
      {label}
    </button>
  );
}

/** Pane classes for a list/detail grid: which pane a phone sees depends on `open`. */
export function paneClasses(open: boolean) {
  return {
    list: open ? "hidden md:flex" : "flex",
    detail: open ? "" : "hidden md:block",
  };
}
