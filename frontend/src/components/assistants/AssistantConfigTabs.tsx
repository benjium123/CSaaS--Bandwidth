import * as React from "react";
import type { AssistantTool, AssistantToolName, PostCallField } from "@/api/assistants";
import {
  Button,
  Card,
  Drawer,
  EmptyState,
  Input,
  Select,
} from "@/components/ui/primitives";

export const ASSISTANT_TOOLS: {
  type: AssistantToolName;
  label: string;
  description: string;
}[] = [
  {
    type: "book_appointment",
    label: "Book appointments",
    description: "Puts a time on your calendar while the call is still going.",
  },
  {
    type: "transfer",
    label: "Transfer to a person",
    description: "Hands the call to a teammate or a queue.",
  },
  {
    type: "send_followup_sms",
    label: "Send a follow-up text",
    description: "Texts the caller after the call ends.",
  },
  {
    type: "lookup_contact",
    label: "Look up the caller",
    description: "Reads what you already know about whoever is calling.",
  },
  // The wire type stays "webhook" for this item; the customer-facing label never says
  // that word because "address you own" says what it does.
  {
    type: "webhook",
    label: "Send information to your own system",
    description: "Posts what the call collected to an address you own.",
  },
];

export function ToolsTab({
  tools,
  onChange,
  onSave,
  saving,
  status,
}: {
  tools: AssistantTool[];
  onChange: (next: AssistantTool[]) => void;
  onSave: () => void;
  saving: boolean;
  status: React.ReactNode;
}) {
  const [drawerOpen, setDrawerOpen] = React.useState(false);
  const [drawerUrl, setDrawerUrl] = React.useState("");
  const [drawerSecret, setDrawerSecret] = React.useState("");
  const [missingAddress, setMissingAddress] = React.useState(false);

  function openDrawer() {
    const existing = tools.find((tool) => tool.tool === "webhook");
    setDrawerUrl(existing?.url ?? "");
    setDrawerSecret("");
    setMissingAddress(false);
    setDrawerOpen(true);
  }

  function closeDrawer() {
    setDrawerOpen(false);
    setMissingAddress(false);
  }

  function toggleTool(entry: (typeof ASSISTANT_TOOLS)[number]) {
    const active = tools.some((tool) => tool.tool === entry.type);
    if (active) {
      onChange(tools.filter((tool) => tool.tool !== entry.type));
      return;
    }

    if (entry.type === "webhook") {
      // The address lives on the tool entry itself, so an entry that is OFF has no address
      // to switch back on with: turning this one on always goes through the drawer. A tool
      // the worker cannot call is worse than a tool that is off.
      setDrawerUrl("");
      setDrawerSecret("");
      setMissingAddress(true);
      setDrawerOpen(true);
      return;
    }

    onChange([...tools, { tool: entry.type }]);
  }

  function handleDrawerDone() {
    const url = drawerUrl.trim();
    if (!url) {
      setMissingAddress(true);
      return;
    }

    const entry: AssistantTool = { tool: "webhook", url };
    // A blank secret means "keep the stored one" - it must NEVER overwrite it.
    if (drawerSecret.trim() !== "") {
      entry.secret = drawerSecret.trim();
    }

    onChange([
      ...tools.filter((tool) => tool.tool !== "webhook"),
      entry,
    ]);
    closeDrawer();
  }

  const storedSecret = Boolean(
    tools.find((tool) => tool.tool === "webhook")?.secret,
  );

  return (
    <div className="space-y-4">
      <div className="space-y-3">
        {ASSISTANT_TOOLS.map((entry) => {
          const active = tools.some((tool) => tool.tool === entry.type);
          return (
            /* No role="group" with the tool's name on it: the switch inside already carries
               that exact accessible name, and a landmark sharing a control's name makes
               every by-name query ambiguous and says the phrase twice in a screen reader. */
            <Card key={entry.type} className="space-y-3">
              <div className="flex items-start justify-between gap-3">
                <div className="min-w-0">
                  <p className="text-sm font-medium">{entry.label}</p>
                  <p className="text-xs text-muted-foreground">{entry.description}</p>
                </div>
                <div className="flex shrink-0 items-center gap-2">
                  {entry.type === "webhook" && (
                    <Button
                      type="button"
                      variant="outline"
                      size="sm"
                      aria-label={`Set up ${entry.label}`}
                      onClick={openDrawer}
                    >
                      Set up
                    </Button>
                  )}
                  <Button
                    type="button"
                    role="switch"
                    aria-checked={active}
                    aria-label={entry.label}
                    variant={active ? "default" : "outline"}
                    size="sm"
                    onClick={() => toggleTool(entry)}
                  >
                    {active ? "On" : "Off"}
                  </Button>
                </div>
              </div>
            </Card>
          );
        })}
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <Button type="button" onClick={onSave} disabled={saving}>
          Save tools
        </Button>
        {status}
      </div>

      <Drawer
        open={drawerOpen}
        onClose={closeDrawer}
        title="Send information to your own system"
        footer={
          <Button type="button" onClick={handleDrawerDone}>
            Done
          </Button>
        }
      >
        <div className="space-y-4">
          <div className="space-y-1">
            <label htmlFor="tool-webhook-url" className="block text-xs text-muted-foreground">
              Address
            </label>
            <Input
              id="tool-webhook-url"
              type="url"
              aria-label="Address"
              placeholder="https://…"
              value={drawerUrl}
              onChange={(event) => setDrawerUrl(event.target.value)}
            />
          </div>
          <div className="space-y-1">
            <label htmlFor="tool-webhook-secret" className="block text-xs text-muted-foreground">
              Signing secret
            </label>
            <Input
              id="tool-webhook-secret"
              type="password"
              aria-label="Signing secret"
              placeholder={storedSecret ? "stored — leave blank to keep" : undefined}
              value={drawerSecret}
              onChange={(event) => setDrawerSecret(event.target.value)}
            />
          </div>
          {missingAddress && drawerUrl.trim() === "" && (
            <p className="text-destructive text-xs">Add an address first.</p>
          )}
        </div>
      </Drawer>
    </div>
  );
}

/**
 * A number field that keeps what the customer TYPED, not what the number rounds to.
 *
 * A plain controlled `value={someNumber}` cannot be cleared: clearing sends "", the parent
 * falls back to its minimum, and the field immediately re-renders as "1" - so typing "5"
 * after a clear silently produced "15". This holds the text locally, reports a parsed
 * number only when the text parses, and re-seeds itself when the value changes from outside.
 */
function NumberField({
  id,
  label,
  value,
  min,
  toText,
  fromText,
}: {
  id: string;
  label: string;
  value: number;
  min: number;
  /** number on the wire -> what the field shows (e.g. seconds -> minutes) */
  toText: (value: number) => string;
  /** what the field shows -> number on the wire; only called when the text parses */
  fromText: (typed: number) => void;
}) {
  const seeded = toText(value);
  const [text, setText] = React.useState(seeded);

  React.useEffect(() => {
    setText((current) => (Number(current) === Number(seeded) ? current : seeded));
  }, [seeded]);

  return (
    <div className="space-y-1">
      <label htmlFor={id} className="block text-xs text-muted-foreground">
        {label}
      </label>
      <Input
        id={id}
        type="number"
        min={min}
        aria-label={label}
        value={text}
        onChange={(event) => {
          const next = event.target.value;
          setText(next);
          const parsed = Number(next);
          // An empty or half-typed field keeps the last good number rather than writing a
          // fallback the customer never asked for.
          if (next.trim() !== "" && Number.isFinite(parsed) && parsed >= min) {
            fromText(Math.floor(parsed));
          }
        }}
      />
    </div>
  );
}

/** The behaviour tab owns the call limits AND the four texting settings the old agent form
 * had. They are here rather than dropped: the SMS agent (P10) is live, and losing its only
 * editor would have been a silent feature regression dressed up as a redesign. */
export type BehaviourValue = {
  max_call_seconds: number;
  silence_timeout_seconds: number;
  interrupt_sensitivity: string;
  voicemail_action: string;
  sms_enabled: boolean;
  sms_turn_ceiling: number;
  sms_max_reply_chars: number;
  sms_handoff_keywords: string[];
};

export function BehaviourTab({
  value,
  onChange,
  onSave,
  saving,
  status,
}: {
  value: BehaviourValue;
  onChange: (next: BehaviourValue) => void;
  onSave: () => void;
  saving: boolean;
  status: React.ReactNode;
}) {
  // Handoff words are a string[] on the wire, edited here as one comma-separated field and
  // held as its own state so a trailing ", " while typing is not collapsed away.
  const [keywordsInput, setKeywordsInput] = React.useState(
    value.sms_handoff_keywords.join(", "),
  );
  const joinedKeywords = value.sms_handoff_keywords.join(", ");
  React.useEffect(() => {
    setKeywordsInput((current) => {
      const parsed = current
        .split(",")
        .map((word) => word.trim())
        .filter(Boolean)
        .join(", ");
      return parsed === joinedKeywords ? current : joinedKeywords;
    });
  }, [joinedKeywords]);

  return (
    <div className="space-y-4">
      {/* Minutes on screen, seconds on the wire. */}
      <NumberField
        id="behaviour-max-minutes"
        label="How long a call can last (minutes)"
        value={value.max_call_seconds}
        min={1}
        toText={(seconds) => String(seconds > 0 ? Math.round(seconds / 60) : 1)}
        fromText={(minutes) => onChange({ ...value, max_call_seconds: minutes * 60 })}
      />

      <NumberField
        id="behaviour-silence-seconds"
        label="Hang up after silence (seconds)"
        value={value.silence_timeout_seconds}
        min={1}
        toText={(seconds) => String(seconds)}
        fromText={(seconds) => onChange({ ...value, silence_timeout_seconds: seconds })}
      />

      <div className="space-y-1">
        <label htmlFor="behaviour-interrupt" className="block text-xs text-muted-foreground">
          Let the caller interrupt
        </label>
        <Select
          id="behaviour-interrupt"
          aria-label="Let the caller interrupt"
          value={value.interrupt_sensitivity}
          onChange={(event) =>
            onChange({ ...value, interrupt_sensitivity: event.target.value })
          }
        >
          <option value="low">Rarely</option>
          <option value="medium">Sometimes</option>
          <option value="high">Easily</option>
        </Select>
      </div>

      <div className="space-y-1">
        <label htmlFor="behaviour-voicemail" className="block text-xs text-muted-foreground">
          If it reaches voicemail
        </label>
        <Select
          id="behaviour-voicemail"
          aria-label="If it reaches voicemail"
          value={value.voicemail_action}
          onChange={(event) =>
            onChange({ ...value, voicemail_action: event.target.value })
          }
        >
          <option value="leave_message">Leave a message</option>
          <option value="hang_up">Hang up</option>
          <option value="retry_later">Try again later</option>
        </Select>
      </div>

      <fieldset className="space-y-3 rounded-md border border-border p-3">
        <legend className="px-1 text-xs font-medium text-muted-foreground">Texting</legend>

        <label className="flex items-center gap-2 text-sm">
          <input
            type="checkbox"
            checked={value.sms_enabled}
            onChange={(event) => onChange({ ...value, sms_enabled: event.target.checked })}
          />
          Reply to inbound texts automatically
        </label>

        <div className="grid gap-3 md:grid-cols-2">
          <NumberField
            id="behaviour-sms-turns"
            label="Most replies in one conversation"
            value={value.sms_turn_ceiling}
            min={1}
            toText={(count) => String(count)}
            fromText={(count) => onChange({ ...value, sms_turn_ceiling: count })}
          />
          <NumberField
            id="behaviour-sms-chars"
            label="Longest reply (characters)"
            value={value.sms_max_reply_chars}
            min={1}
            toText={(count) => String(count)}
            fromText={(count) => onChange({ ...value, sms_max_reply_chars: count })}
          />
        </div>

        <div className="space-y-1">
          <label htmlFor="behaviour-sms-handoff" className="block text-xs text-muted-foreground">
            Words that hand the conversation to a person
          </label>
          <Input
            id="behaviour-sms-handoff"
            aria-label="Words that hand the conversation to a person"
            placeholder="human, agent, representative"
            value={keywordsInput}
            onChange={(event) => {
              const text = event.target.value;
              setKeywordsInput(text);
              onChange({
                ...value,
                sms_handoff_keywords: text
                  .split(",")
                  .map((word) => word.trim())
                  .filter(Boolean),
              });
            }}
          />
          <p className="text-[11px] text-muted-foreground">Separate them with commas.</p>
        </div>
      </fieldset>

      <div className="flex flex-wrap items-center gap-3">
        <Button type="button" onClick={onSave} disabled={saving}>
          Save behaviour
        </Button>
        {status}
      </div>
    </div>
  );
}

const CONFIRM_TIMEOUT_MS = 8000;

function OutcomeFieldRow({
  field,
  index,
  onChange,
  onRemove,
}: {
  field: PostCallField;
  index: number;
  onChange: (next: PostCallField) => void;
  onRemove: () => void;
}) {
  // Options are kept as a local string so a trailing ", " while typing is not collapsed
  // by round-tripping through PostCallField.options on every keystroke (same trick the
  // old AgentPage used for keywords).
  const [optionsInput, setOptionsInput] = React.useState(
    field.options?.join(", ") ?? "",
  );
  const [confirmingRemove, setConfirmingRemove] = React.useState(false);
  const removeTimerRef = React.useRef<ReturnType<typeof setTimeout> | null>(null);

  // Rows are keyed by POSITION, so removing a row hands this same component a DIFFERENT
  // field - without this, row 2's choices would stay on screen after row 1 was removed.
  // It re-seeds only when the incoming options genuinely differ from what the local text
  // parses to, which is what keeps a half-typed "yes, " from being collapsed mid-keystroke.
  const joinedOptions = field.options?.join(", ") ?? "";
  React.useEffect(() => {
    setOptionsInput((current) => {
      const parsed = current
        .split(",")
        .map((option) => option.trim())
        .filter(Boolean)
        .join(", ");
      return parsed === joinedOptions ? current : joinedOptions;
    });
  }, [joinedOptions]);

  React.useEffect(() => {
    return () => {
      if (removeTimerRef.current) clearTimeout(removeTimerRef.current);
    };
  }, []);

  function handleRemoveClick() {
    if (confirmingRemove) {
      if (removeTimerRef.current) clearTimeout(removeTimerRef.current);
      onRemove();
      return;
    }

    setConfirmingRemove(true);
    if (removeTimerRef.current) clearTimeout(removeTimerRef.current);
    removeTimerRef.current = setTimeout(
      () => setConfirmingRemove(false),
      CONFIRM_TIMEOUT_MS,
    );
  }

  return (
    <Card className="space-y-3">
      <div className="grid gap-3 md:grid-cols-2">
        <div className="space-y-1">
          <label htmlFor={`outcome-name-${index}`} className="block text-xs text-muted-foreground">
            Name
          </label>
          <Input
            id={`outcome-name-${index}`}
            aria-label={`Field name ${index + 1}`}
            value={field.name}
            onChange={(event) => onChange({ ...field, name: event.target.value })}
          />
        </div>

        <div className="space-y-1">
          <label htmlFor={`outcome-type-${index}`} className="block text-xs text-muted-foreground">
            Type
          </label>
          <Select
            id={`outcome-type-${index}`}
            aria-label={`Field type ${index + 1}`}
            value={field.type}
            onChange={(event) =>
              onChange({ ...field, type: event.target.value as PostCallField["type"] })
            }
          >
            <option value="text">Text</option>
            <option value="number">Number</option>
            <option value="date">Date</option>
            <option value="select">Choice</option>
          </Select>
        </div>

        {field.type === "select" && (
          <div className="space-y-1 md:col-span-2">
            <label htmlFor={`outcome-choices-${index}`} className="block text-xs text-muted-foreground">
              Choices
            </label>
            <Input
              id={`outcome-choices-${index}`}
              aria-label={`Choices ${index + 1}`}
              value={optionsInput}
              onChange={(event) => {
                const text = event.target.value;
                setOptionsInput(text);
                onChange({
                  ...field,
                  options: text
                    .split(",")
                    .map((option) => option.trim())
                    .filter(Boolean),
                });
              }}
            />
          </div>
        )}

        <div className="space-y-1 md:col-span-2">
          <label htmlFor={`outcome-contact-${index}`} className="block text-xs text-muted-foreground">
            Save to contact detail
          </label>
          <Input
            id={`outcome-contact-${index}`}
            aria-label={`Save to contact detail ${index + 1}`}
            value={field.write_to_attribute ?? ""}
            onChange={(event) =>
              onChange({ ...field, write_to_attribute: event.target.value })
            }
          />
          <p className="text-[11px] text-muted-foreground">Optional.</p>
        </div>
      </div>

      <div className="flex justify-end">
        <Button
          type="button"
          variant="destructive"
          size="sm"
          aria-label={
            confirmingRemove ? `Confirm remove field ${index + 1}` : `Remove field ${index + 1}`
          }
          onClick={handleRemoveClick}
        >
          {confirmingRemove ? "Confirm remove?" : "Remove"}
        </Button>
      </div>
    </Card>
  );
}

export function OutcomesTab({
  fields,
  onChange,
  onSave,
  saving,
  status,
}: {
  fields: PostCallField[];
  onChange: (next: PostCallField[]) => void;
  onSave: () => void;
  saving: boolean;
  status: React.ReactNode;
}) {
  function updateFieldAt(index: number, next: PostCallField) {
    onChange(fields.map((field, itemIndex) => (itemIndex === index ? next : field)));
  }

  function removeFieldAt(index: number) {
    onChange(fields.filter((_, itemIndex) => itemIndex !== index));
  }

  function addField() {
    onChange([...fields, { name: "", type: "text" }]);
  }

  return (
    <div className="space-y-4">
      <p className="text-sm text-muted-foreground">What every call should collect.</p>

      {fields.length === 0 ? (
        <EmptyState
          title="Nothing collected yet"
          description="Add a field and your assistant will collect it on every call."
        />
      ) : (
        <ul aria-label="Collected fields" className="space-y-3">
          {fields.map((field, index) => (
            <li key={index}>
              <OutcomeFieldRow
                field={field}
                index={index}
                onChange={(next) => updateFieldAt(index, next)}
                onRemove={() => removeFieldAt(index)}
              />
            </li>
          ))}
        </ul>
      )}

      <div className="flex flex-wrap items-center gap-3">
        <Button type="button" variant="outline" onClick={addField}>
          Add a field
        </Button>
      </div>

      <div className="flex flex-wrap items-center gap-3">
        <Button type="button" onClick={onSave} disabled={saving}>
          Save outcomes
        </Button>
        {status}
      </div>
    </div>
  );
}
