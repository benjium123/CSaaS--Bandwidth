import * as React from "react";
import { useMutation } from "@tanstack/react-query";
import { useAuth } from "@/auth/AuthContext";
import { simulateAssistant, type SimulateOut, type SimulateTurn } from "@/api/assistants";
import { Button, Drawer, EmptyState, Input, MutationStatus } from "@/components/ui/primitives";

export function SimulatorDrawer({
  open,
  onClose,
  assistantId,
  assistantName,
}: {
  open: boolean;
  onClose: () => void;
  assistantId: string | null;
  assistantName: string;
}) {
  const { api } = useAuth();
  const [turns, setTurns] = React.useState<SimulateTurn[]>([]);
  const [draft, setDraft] = React.useState("");
  const [lastReply, setLastReply] = React.useState<SimulateOut | null>(null);

  const isOpenRef = React.useRef(open);

  React.useEffect(() => {
    isOpenRef.current = open;
    if (!open) {
      // Reset the conversation whenever the drawer closes so re-opening never shows a
      // stale transcript.
      setTurns([]);
      setDraft("");
      setLastReply(null);
    }
  }, [open]);

  const simulationMutation = useMutation({
    mutationFn: async (messages: SimulateTurn[]) => {
      if (assistantId === null) {
        throw new Error("Save your assistant first.");
      }
      return simulateAssistant(api, assistantId, messages);
    },
    onSuccess: (reply) => {
      if (!isOpenRef.current) return;
      setTurns((prev) => [...prev, { role: "assistant", content: reply.reply }]);
      setLastReply(reply);
    },
  });

  function handleSubmit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const content = draft.trim();
    if (!content || assistantId === null) return;

    const userTurn: SimulateTurn = { role: "user", content };
    // The endpoint is stateless: each request carries every previous turn plus the new one.
    const nextTurns = [...turns, userTurn];
    setTurns(nextTurns);
    setDraft("");
    setLastReply(null);
    // Keep the typed turn in local state before the request, so a failed send does not
    // throw the customer's words away.
    simulationMutation.mutate(nextTurns);
  }

  const canSend = draft.trim() !== "" && assistantId !== null && !simulationMutation.isPending;

  return (
    <Drawer open={open} onClose={onClose} title="Test your assistant">
      {assistantId === null ? (
        <EmptyState
          title="Save your assistant first"
          description="A test answers with the instructions you have saved, so save before you try it."
        />
      ) : (
        <div className="space-y-4">
          <p className="text-sm text-muted-foreground">
            Talking to {assistantName}. Nobody is called and no message is sent.
          </p>

          {turns.length === 0 ? (
            <p className="text-sm text-muted-foreground">
              Say something and see how it answers.
            </p>
          ) : (
            <ul aria-label="Conversation" className="space-y-3">
              {turns.map((turn, index) => (
                <li key={`${index}-${turn.role}`} className="space-y-1">
                  <span className="text-xs text-muted-foreground">
                    {turn.role === "user" ? "You" : "Assistant"}
                  </span>
                  <p className="text-sm">{turn.content}</p>
                </li>
              ))}
            </ul>
          )}

          <form onSubmit={handleSubmit} className="space-y-3">
            <Input
              aria-label="Your message"
              value={draft}
              onChange={(event) => setDraft(event.target.value)}
            />
            <div className="flex items-center gap-3">
              <Button type="submit" disabled={!canSend}>
                Send
              </Button>
              <MutationStatus
                pending={simulationMutation.isPending}
                error={simulationMutation.error}
                pendingLabel="Answering…"
              />
            </div>
          </form>

          {lastReply && (
            <div className="space-y-2 border-t border-border pt-3 text-xs text-muted-foreground">
              <p>
                Tokens: {lastReply.tokens_in} in, {lastReply.tokens_out} out.
              </p>
              {lastReply.kb_hits.length > 0 && (
                <>
                  <p>From your knowledge:</p>
                  <ul aria-label="Knowledge used" className="space-y-1">
                    {lastReply.kb_hits.map((hit) => (
                      <li key={hit.document_id}>
                        <span>{hit.title}</span>
                        {hit.snippet ? (
                          <span className="block text-xs text-muted-foreground">
                            {hit.snippet}
                          </span>
                        ) : null}
                      </li>
                    ))}
                  </ul>
                </>
              )}
            </div>
          )}
        </div>
      )}
    </Drawer>
  );
}
