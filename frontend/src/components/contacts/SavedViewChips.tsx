/**
 * The "Views" row above the contacts table (Phase 27).
 *
 * A saved view is a name for a set of contact filters. The row follows the same rule as
 * the inbox's FilterChips: past MAX_VISIBLE_CHIPS entries a row of choices stops being a
 * row and becomes noise, so it collapses into a single menu.
 */
import * as React from "react";
import { useAuth } from "@/auth/AuthContext";
import { Button, Input, Badge, Spinner } from "@/components/ui/primitives";
import { getErrorMessage } from "@/api/contacts";
import { MAX_VISIBLE_CHIPS } from "@/components/conversations/ConversationList";
import {
  useSavedViews,
  useCreateSavedView,
  useDeleteSavedView,
  type SavedView,
  type ContactExportFilters,
} from "@/api/contactsPro";
import { cn } from "@/lib/utils";

export function SavedViewChips({
  activeViewId,
  onApply,
  currentFilters,
  canShare,
}: {
  activeViewId: string | null;
  /** null clears the active view (back to the raw filters the user typed). */
  onApply: (view: SavedView | null) => void;
  /** The filters the page is showing right now, saved verbatim when the user saves a view. */
  currentFilters: ContactExportFilters;
  /** contacts:write - only then may a view be shared with the whole workspace. */
  canShare: boolean;
}) {
  const { api } = useAuth();
  const viewsQuery = useSavedViews(api);
  const createMutation = useCreateSavedView(api);
  const deleteMutation = useDeleteSavedView(api);

  const [menuOpen, setMenuOpen] = React.useState(false);
  const menuRef = React.useRef<HTMLDivElement>(null);

  const [saveOpen, setSaveOpen] = React.useState(false);
  const [viewName, setViewName] = React.useState("");
  const [shared, setShared] = React.useState(false);
  const [saveError, setSaveError] = React.useState<string | null>(null);

  const [deleteError, setDeleteError] = React.useState<string | null>(null);
  const [confirmDelete, setConfirmDelete] = React.useState(false);

  const views = viewsQuery.data ?? [];

  const sortedViews = React.useMemo(() => {
    const privateViews = views.filter((view) => !view.shared);
    const sharedViews = views.filter((view) => view.shared);
    return [...privateViews, ...sharedViews];
  }, [views]);

  const activeView = sortedViews.find((view) => view.id === activeViewId);

  React.useEffect(() => {
    if (!menuOpen) return;

    function handleMouseDown(event: MouseEvent) {
      const target = event.target;
      if (target instanceof Node && menuRef.current && !menuRef.current.contains(target)) {
        setMenuOpen(false);
      }
    }

    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        setMenuOpen(false);
      }
    }

    document.addEventListener("mousedown", handleMouseDown);
    document.addEventListener("keydown", handleKeyDown);

    return () => {
      document.removeEventListener("mousedown", handleMouseDown);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [menuOpen]);

  if (viewsQuery.isLoading) {
    return <Spinner label="Loading views" />;
  }

  const collapsed = sortedViews.length > MAX_VISIBLE_CHIPS;
  const collapseTriggerLabel = activeView ? `View: ${activeView.name}` : "Views";

  function handleViewClick(view: SavedView) {
    if (activeViewId === view.id) {
      onApply(null);
    } else {
      onApply(view);
    }
    setMenuOpen(false);
  }

  function handleSave(event: React.FormEvent) {
    event.preventDefault();
    const trimmedName = viewName.trim();
    if (!trimmedName) return;

    setSaveError(null);
    const filtersToSave: Record<string, unknown> = { ...currentFilters };

    createMutation.mutate(
      {
        name: trimmedName,
        filters: filtersToSave,
        shared,
      },
      {
        onSuccess: (createdView) => {
          setSaveOpen(false);
          setViewName("");
          setShared(false);
          onApply(createdView);
        },
        onError: (error) => {
          setSaveError(getErrorMessage(error));
        },
      },
    );
  }

  function handleDelete() {
    if (!activeView) return;

    setDeleteError(null);
    deleteMutation.mutate(
      { viewId: activeView.id },
      {
        onSuccess: () => {
          setConfirmDelete(false);
          onApply(null);
        },
        onError: (error) => {
          setDeleteError(getErrorMessage(error));
        },
      },
    );
  }

  const canDeleteActiveView = activeView !== undefined && !(activeView.shared && !canShare);

  return (
    <div className="flex flex-wrap items-center gap-2" role="group" aria-label="Saved views">
      {viewsQuery.isError ? (
        <p role="alert" className="text-sm text-destructive">
          {getErrorMessage(viewsQuery.error)}
        </p>
      ) : null}

      {!viewsQuery.isError && collapsed ? (
        <div className="relative" ref={menuRef}>
          <Button
            type="button"
            variant="outline"
            size="sm"
            aria-haspopup="menu"
            aria-expanded={menuOpen}
            onClick={() => setMenuOpen((prev) => !prev)}
          >
            {collapseTriggerLabel}
          </Button>
          {menuOpen ? (
            <div
              role="menu"
              aria-label="Choose a saved view"
              className="absolute z-10 mt-1 w-64 rounded-md border border-border bg-background p-1 shadow-md"
            >
              {sortedViews.map((view) => (
                <button
                  key={view.id}
                  type="button"
                  role="menuitemradio"
                  aria-checked={activeViewId === view.id}
                  onClick={() => handleViewClick(view)}
                  className="flex w-full items-center rounded px-2 py-1.5 text-left text-sm text-foreground hover:bg-muted"
                >
                  {view.name}
                  {view.shared ? " - Shared" : null}
                </button>
              ))}
            </div>
          ) : null}
        </div>
      ) : null}

      {!viewsQuery.isError && !collapsed
        ? sortedViews.map((view) => (
            <button
              key={view.id}
              type="button"
              aria-pressed={activeViewId === view.id}
              onClick={() => handleViewClick(view)}
              className={cn(
                "inline-flex items-center gap-1 rounded-full px-2.5 py-1 text-xs font-medium",
                activeViewId === view.id
                  ? "bg-primary text-primary-foreground"
                  : "border border-border text-muted-foreground hover:bg-muted",
              )}
            >
              <span>{view.name}</span>
              {view.shared ? (
                <Badge className="border border-current px-1 py-0">Shared</Badge>
              ) : null}
            </button>
          ))
        : null}

      {saveOpen ? (
        <form className="flex flex-wrap items-center gap-2" onSubmit={handleSave}>
          <Input
            aria-label="View name"
            value={viewName}
            onChange={(event) => setViewName(event.target.value)}
          />
          {canShare ? (
            <label className="flex items-center gap-1.5 text-sm text-muted-foreground">
              <input
                type="checkbox"
                checked={shared}
                onChange={(event) => setShared(event.target.checked)}
              />
              Share with the team
            </label>
          ) : null}
          <Button
            type="submit"
            size="sm"
            disabled={viewName.trim().length === 0 || createMutation.isPending}
          >
            Save
          </Button>
          <Button
            type="button"
            size="sm"
            variant="ghost"
            onClick={() => {
              setSaveOpen(false);
              setSaveError(null);
              setViewName("");
              setShared(false);
            }}
          >
            Cancel
          </Button>
          {saveError ? (
            <p role="alert" className="text-sm text-destructive">
              {saveError}
            </p>
          ) : null}
        </form>
      ) : (
        <Button type="button" size="sm" variant="outline" onClick={() => setSaveOpen(true)}>
          Save current filters
        </Button>
      )}

      {canDeleteActiveView && activeView ? (
        <div className="flex items-center gap-2">
          {confirmDelete ? (
            <>
              <Button
                type="button"
                size="sm"
                variant="destructive"
                disabled={deleteMutation.isPending}
                onClick={handleDelete}
              >
                Delete this view?
              </Button>
              <Button
                type="button"
                size="sm"
                variant="ghost"
                onClick={() => setConfirmDelete(false)}
              >
                Cancel
              </Button>
            </>
          ) : (
            <Button type="button" size="sm" variant="ghost" onClick={() => setConfirmDelete(true)}>
              Delete view
            </Button>
          )}
          {deleteError ? (
            <p role="alert" className="text-sm text-destructive">
              {deleteError}
            </p>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
