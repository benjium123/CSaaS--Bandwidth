/**
 * Floating chat assistant for the marketing site.
 *
 * Answers questions from the shared FAQ list first, falls back to the backend assistant, and can
 * hand the conversation to the team (leaving a form, then polling the live chat for replies).
 * All styling lives in the `ms-chat-*` classes in `marketing/site.css`.
 */
import * as React from "react";
import { useLocation } from "react-router-dom";
import { MessageCircle, SendHorizontal, X } from "lucide-react";
import { useAuth } from "@/auth/AuthContext";
import { faqsFor, matchFaq, relevantFaqs } from "@/marketing/faq";

type Role = "visitor" | "assistant" | "agent" | "system";
type Mode = "bot" | "form" | "live";

interface ChatMessage {
  id: string;
  role: Role;
  text: string;
  at?: string;
}

interface AskResponse {
  answer: string;
  handoff: boolean;
}

interface HandoffResponse {
  chat_id: string;
  token: string;
  staffed: boolean;
}

interface LiveMessage {
  id: string;
  role: "agent" | "visitor";
  text: string;
  at: string;
}

interface LiveResponse {
  status: "waiting" | "active" | "closed";
  agent_name: string | null;
  messages: LiveMessage[];
}

const GREETING = "Hi! I can answer questions about plans, pricing, numbers, texting and signup. Ask me anything, or ask for a person at any time.";
const PERSON_CHIP = "Talk to a person";
const CHIPS = ["How much is it?", "How do minutes work?", "Can I keep my number?", "How does texting work?", PERSON_CHIP];
const TEASER_TEXT = "Questions about pricing? Ask me.";
const FRUSTRATION = /(not helping|useless|speak to|real person|human|agent)/i;
const EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const TEASER_KEY = "rl-chat-teaser";
const LIVE_KEY = "rl-chat-live";
const TEASER_DELAY = 8000;
const POLL_MS = 4000;
const PANEL_ID = "rl-chat-panel";

let sequence = 0;
function nextId(): string {
  sequence += 1;
  return `rl-chat-${sequence}`;
}

function readSession(key: string): string | null {
  try {
    return window.sessionStorage.getItem(key);
  } catch {
    return null;
  }
}

function writeSession(key: string, value: string): void {
  try {
    window.sessionStorage.setItem(key, value);
  } catch {
    /* storage can be blocked or full; the chat still works without it */
  }
}

function clearSession(key: string): void {
  try {
    window.sessionStorage.removeItem(key);
  } catch {
    /* storage can be blocked; nothing to clean up then */
  }
}

export function ChatWidget() {
  const { api } = useAuth();
  const location = useLocation();

  const launcherRef = React.useRef<HTMLButtonElement>(null);
  const inputRef = React.useRef<HTMLInputElement>(null);
  const logRef = React.useRef<HTMLDivElement>(null);
  const openedRef = React.useRef(false);
  const busyRef = React.useRef(false);
  const missesRef = React.useRef(0);
  const reasonRef = React.useRef("asked");
  const lastAtRef = React.useRef("");
  const apiRef = React.useRef(api);

  const [open, setOpen] = React.useState(false);
  const [teaser, setTeaser] = React.useState(false);
  const [messages, setMessages] = React.useState<ChatMessage[]>([]);
  const [chips, setChips] = React.useState<string[]>(CHIPS);
  const [mode, setMode] = React.useState<Mode>("bot");
  const [typing, setTyping] = React.useState(false);
  const [draft, setDraft] = React.useState("");
  const [chatId, setChatId] = React.useState<string | null>(null);
  const [token, setToken] = React.useState<string | null>(null);
  const [agentName, setAgentName] = React.useState<string | null>(null);
  const [name, setName] = React.useState("");
  const [email, setEmail] = React.useState("");
  const [phone, setPhone] = React.useState("");
  const [smsConsent, setSmsConsent] = React.useState(false);
  const [formError, setFormError] = React.useState<string | null>(null);
  const [submitting, setSubmitting] = React.useState(false);

  React.useEffect(() => {
    apiRef.current = api;
  }, [api]);

  const push = React.useCallback((role: Role, text: string, at?: string) => {
    setMessages(prev => [...prev, { id: nextId(), role, text, at }]);
  }, []);

  const openForm = React.useCallback((reason: string) => {
    reasonRef.current = reason;
    setChips([]);
    setMode("form");
  }, []);

  // Teaser bubble: once per session, eight seconds after the page settles.
  React.useEffect(() => {
    if (open || teaser) return;
    if (readSession(TEASER_KEY)) return;
    const timer = window.setTimeout(() => {
      writeSession(TEASER_KEY, "1");
      setTeaser(true);
    }, TEASER_DELAY);
    return () => window.clearTimeout(timer);
  }, [open, teaser]);

  // Greeting on first open, while we are still just the assistant.
  React.useEffect(() => {
    if (!open) return;
    if (mode !== "bot") return;
    setMessages(prev => (prev.length > 0 ? prev : [{ id: nextId(), role: "assistant", text: GREETING }]));
  }, [open, mode]);

  // Focus the composer when the panel opens, hand focus back to the launcher when it closes.
  React.useEffect(() => {
    if (open) {
      openedRef.current = true;
      inputRef.current?.focus();
      return;
    }
    if (openedRef.current) {
      openedRef.current = false;
      launcherRef.current?.focus();
    }
  }, [open]);

  React.useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open]);

  React.useEffect(() => {
    const log = logRef.current;
    if (!log) return;
    log.scrollTop = log.scrollHeight;
  }, [messages, typing, mode, open]);

  // Resume a live conversation the visitor already started in this session.
  React.useEffect(() => {
    const stored = readSession(LIVE_KEY);
    if (!stored) return;
    try {
      const parsed: unknown = JSON.parse(stored);
      if (typeof parsed !== "object" || parsed === null) return;
      const value = parsed as { chatId?: unknown; token?: unknown };
      if (typeof value.chatId !== "string" || typeof value.token !== "string") return;
      setChatId(value.chatId);
      setToken(value.token);
      setMode("live");
    } catch {
      clearSession(LIVE_KEY);
    }
  }, []);

  // Live polling: only while the tab is visible, every four seconds.
  React.useEffect(() => {
    if (mode !== "live" || !chatId || !token) return;
    let stopped = false;
    let inFlight = false;

    const poll = async () => {
      if (stopped || inFlight) return;
      if (typeof document !== "undefined" && document.visibilityState !== "visible") return;
      inFlight = true;
      try {
        const path = `/api/v1/public/site-chat/${encodeURIComponent(chatId)}/messages?token=${encodeURIComponent(token)}&after=${encodeURIComponent(lastAtRef.current)}`;
        const res = await apiRef.current.request<LiveResponse>(path, { method: "GET" });
        if (stopped) return;
        if (res.agent_name) setAgentName(res.agent_name);
        if (res.messages.length > 0) {
          setMessages(prev => {
            const known = new Set(prev.map(message => message.id));
            const fresh = res.messages.filter(message => !known.has(message.id));
            if (fresh.length === 0) return prev;
            return [
              ...prev,
              ...fresh.map(message => ({ id: message.id, role: message.role, text: message.text, at: message.at })),
            ];
          });
          for (const message of res.messages) {
            if (message.at && message.at > lastAtRef.current) lastAtRef.current = message.at;
          }
        }
        if (res.status === "closed") {
          stopped = true;
          clearSession(LIVE_KEY);
          lastAtRef.current = "";
          setAgentName(null);
          setChatId(null);
          setToken(null);
          setMode("bot");
          setChips(CHIPS);
          push("system", "This chat has ended.");
        }
      } catch {
        /* The next tick tries again; the visitor can keep typing meanwhile. */
      } finally {
        inFlight = false;
      }
    };

    void poll();
    const timer = window.setInterval(() => {
      void poll();
    }, POLL_MS);
    return () => {
      stopped = true;
      window.clearInterval(timer);
    };
  }, [mode, chatId, token, push]);

  const send = async (raw: string) => {
    const text = raw.trim();
    if (!text || busyRef.current) return;
    setDraft("");
    setTeaser(false);
    setChips([]);

    if (mode === "live" && chatId && token) {
      push("visitor", text);
      try {
        await api.request(`/api/v1/public/site-chat/${encodeURIComponent(chatId)}/messages`, {
          method: "POST",
          json: { token, text },
        });
      } catch {
        push("system", "That message did not go through. Please try again.");
      }
      return;
    }

    push("visitor", text);

    if (FRUSTRATION.test(text)) {
      push("assistant", "Sure. Let me connect you with the team.");
      openForm("asked");
      return;
    }

    const faq = matchFaq(text);
    if (faq) {
      push("assistant", faq.a);
      missesRef.current = 0;
      if (faq.handoff) {
        openForm(`faq:${faq.id}`);
        return;
      }
      const followUps = faqsFor(faq.topic)
        .filter(other => other.id !== faq.id)
        .slice(0, 3)
        .map(other => other.q);
      setChips([...followUps, PERSON_CHIP]);
      return;
    }

    // Earlier turns only: the current question travels as `question`, not twice.
    const earlier = messages.filter(message => message.role === "visitor" || message.role === "assistant");
    const last = earlier[earlier.length - 1];
    if (last && last.role === "visitor" && last.text === text) earlier.pop();
    const history = earlier
      .slice(-10)
      .map(message => ({ role: message.role as "visitor" | "assistant", text: message.text.slice(0, 1000) }));

    busyRef.current = true;
    setTyping(true);
    try {
      const res = await api.request<AskResponse>("/api/v1/public/site-chat/ask", {
        method: "POST",
        json: { question: text, history, context: relevantFaqs(text) },
      });
      setTyping(false);
      push("assistant", res.answer);
      if (res.handoff) {
        missesRef.current += 1;
        if (missesRef.current >= 2) openForm("stuck");
        else setChips([PERSON_CHIP, "How much is it?"]);
      } else {
        missesRef.current = 0;
      }
    } catch {
      setTyping(false);
      push("assistant", "I couldn't reach the assistant just now.");
      openForm("error");
    } finally {
      busyRef.current = false;
    }
  };

  const submitHandoff = async (event: React.FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (submitting) return;
    const trimmedName = name.trim();
    const trimmedEmail = email.trim();
    if (!trimmedName) {
      setFormError("Please add your name so we know who to reply to.");
      return;
    }
    if (!EMAIL.test(trimmedEmail)) {
      setFormError("Please add an email address we can reply to.");
      return;
    }
    setFormError(null);
    setSubmitting(true);
    try {
      const res = await api.request<HandoffResponse>("/api/v1/public/site-chat/handoff", {
        method: "POST",
        json: {
          name: trimmedName,
          email: trimmedEmail,
          phone: phone.trim() || null,
          sms_consent: smsConsent,
          page: location.pathname,
          reason: reasonRef.current,
          transcript: messages
            .filter(message => message.role === "visitor" || message.role === "assistant")
            .slice(-40)
            .map(message => ({ role: message.role as "visitor" | "assistant", text: message.text.slice(0, 1000) })),
        },
      });
      writeSession(LIVE_KEY, JSON.stringify({ chatId: res.chat_id, token: res.token }));
      lastAtRef.current = "";
      setAgentName(null);
      setChatId(res.chat_id);
      setToken(res.token);
      setMode("live");
      push(
        "system",
        res.staffed
          ? "Connecting you to the team…"
          : "We're offline right now. We'll reply by email, usually within one business day.",
      );
    } catch {
      setFormError("We couldn't send that just now. Please try again in a moment.");
    } finally {
      setSubmitting(false);
    }
  };

  const toggleOpen = () => {
    setTeaser(false);
    writeSession(TEASER_KEY, "1");
    setOpen(current => !current);
  };

  const dismissTeaser = () => {
    setTeaser(false);
    writeSession(TEASER_KEY, "1");
  };

  const status = mode === "live"
    ? agentName
      ? `${agentName} from Ringlite`
      : "Waiting for the team…"
    : "Assistant · replies instantly";

  const renderMessage = (message: ChatMessage) => {
    if (message.role === "system") {
      return <p key={message.id} className="ms-chat-sys">{message.text}</p>;
    }
    if (message.role === "agent") {
      return (
        <div key={message.id} className="rl-bubble incoming">
          <span className="ms-chat-who">{agentName ?? "Ringlite team"}</span>
          {message.text}
        </div>
      );
    }
    return (
      <div key={message.id} className={`rl-bubble ${message.role === "visitor" ? "outgoing" : "incoming"}`}>
        {message.text}
      </div>
    );
  };

  return (
    <>
      {teaser && !open && (
        <div className="ms-chat-teaser">
          <button type="button" className="ms-chat-teaser-open" onClick={toggleOpen}>{TEASER_TEXT}</button>
          <button type="button" className="ms-chat-teaser-close" onClick={dismissTeaser} aria-label="Dismiss chat message"><X size={13} /></button>
        </div>
      )}

      <button
        ref={launcherRef}
        type="button"
        className="ms-chat-launch"
        aria-label={open ? "Close chat" : "Open chat"}
        aria-expanded={open}
        aria-controls={open ? PANEL_ID : undefined}
        onClick={toggleOpen}
      >
        {open ? <X size={20} /> : <MessageCircle size={20} />}
      </button>

      {open && (
        <div className="ms-chat-panel" id={PANEL_ID} role="dialog" aria-label="Ringlite assistant">
          <div className="ms-chat-head">
            <span className="rl-mark" aria-hidden="true"><span /><span /><span /></span>
            <span className="ms-chat-head-text"><strong>Ringlite</strong><small>{status}</small></span>
            <button type="button" className="ms-chat-close" onClick={() => setOpen(false)} aria-label="Close chat"><X size={16} /></button>
          </div>

          <div className="ms-chat-log" ref={logRef} aria-live="polite">
            {messages.map(renderMessage)}
            {typing && <div className="ms-chat-typing" aria-label="The assistant is typing"><i /><i /><i /></div>}
            {mode === "form" && (
              <form className="ms-chat-form" onSubmit={submitHandoff} noValidate>
                <label htmlFor="chat-name">Name</label>
                <input
                  id="chat-name"
                  name="name"
                  value={name}
                  onChange={event => setName(event.target.value)}
                  autoComplete="name"
                  required
                />
                <label htmlFor="chat-email">Email</label>
                <input
                  id="chat-email"
                  name="email"
                  type="email"
                  value={email}
                  onChange={event => setEmail(event.target.value)}
                  autoComplete="email"
                  required
                />
                <label htmlFor="chat-phone">Phone (optional)</label>
                <input
                  id="chat-phone"
                  name="phone"
                  type="tel"
                  value={phone}
                  onChange={event => setPhone(event.target.value)}
                  autoComplete="tel"
                />
                <label className="ms-chat-consent" htmlFor="chat-sms">
                  <input
                    id="chat-sms"
                    name="sms"
                    type="checkbox"
                    checked={smsConsent}
                    onChange={event => setSmsConsent(event.target.checked)}
                  />
                  <span>Text me replies. Message and data rates may apply. Reply STOP to opt out.</span>
                </label>
                {formError && <p className="ms-chat-error" role="alert">{formError}</p>}
                <button type="submit" className="rl-button rl-small" disabled={submitting}>
                  {submitting ? "Connecting…" : "Connect me"}
                </button>
              </form>
            )}
          </div>

          {chips.length > 0 && mode !== "form" && (
            <div className="ms-chat-chips">
              {chips.map(chip => (
                <button key={chip} type="button" onClick={() => { void send(chip); }}>{chip}</button>
              ))}
            </div>
          )}

          <form
            className="ms-chat-compose"
            onSubmit={event => {
              event.preventDefault();
              void send(draft);
            }}
          >
            <label className="ms-sr" htmlFor="chat-input">Message</label>
            <input
              id="chat-input"
              ref={inputRef}
              value={draft}
              onChange={event => setDraft(event.target.value)}
              placeholder="Ask about plans, numbers or texting…"
              autoComplete="off"
            />
            <button type="submit" aria-label="Send"><SendHorizontal size={17} /></button>
          </form>
        </div>
      )}
    </>
  );
}
