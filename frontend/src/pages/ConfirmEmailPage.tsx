import { useState } from "react";
import { useAuth } from "@/auth/AuthContext";
import { Button, mutationErrorMessage } from "@/components/ui/primitives";

export function ConfirmEmailPage() {
  const { api, me, refreshMe, logout } = useAuth();
  const [pending, setPending] = useState(false);
  const [message, setMessage] = useState("");
  const [done, setDone] = useState(false);
  const token = new URLSearchParams(window.location.search).get("token");
  async function act(confirm: boolean) {
    setPending(true); setMessage("");
    try {
      await api.request(`/api/v1/auth/${confirm ? "confirm-email" : "resend-confirmation"}`, { method: "POST", json: confirm ? { token } : {} });
      if (confirm) { setDone(true); window.history.replaceState(null, "", "/confirm-email"); await refreshMe(); }
      else setMessage("Confirmation email sent. Check your inbox and spam folder.");
    } catch (error) { setMessage(mutationErrorMessage(error)); }
    finally { setPending(false); }
  }
  return <main className="flex min-h-screen items-center justify-center bg-gradient-to-br from-blue-50 via-white to-slate-100 p-6">
    <section className="w-full max-w-lg rounded-3xl border border-blue-100 bg-white p-10 shadow-xl shadow-blue-900/5">
      <p className="mb-8 font-semibold text-blue-700">Ringlite</p>
      <div className="mb-5 flex h-14 w-14 items-center justify-center rounded-2xl bg-blue-100 text-2xl text-blue-700">✉</div>
      <h1 className="text-3xl font-semibold tracking-tight">{done ? "Email confirmed" : "Confirm your email"}</h1>
      <p className="my-5 leading-7 text-slate-600">{done ? "Your email address is verified. You can continue setting up your account." : token ? "Confirm your email address to continue with Ringlite." : `Check ${me?.email ?? "your inbox"} for your confirmation link. Confirm your email before starting verification.`}</p>
      {!token && !done && me?.email_confirmation_sent === false && <p role="alert" className="my-4 rounded-xl bg-amber-50 p-4 text-sm text-amber-800">Your account was created, but we could not deliver the confirmation email yet. Please try resending shortly.</p>}
      {message && <p role="status" className="my-4 rounded-xl bg-blue-50 p-4 text-sm">{message}</p>}
      {done ? <Button className="bg-blue-600 text-white hover:bg-blue-700" onClick={() => window.location.assign(me ? "/verification" : "/login")}>Continue</Button> : token ? <Button className="bg-blue-600 text-white hover:bg-blue-700" disabled={pending} onClick={() => void act(true)}>Confirm email address</Button> : <Button className="bg-blue-600 text-white hover:bg-blue-700" disabled={pending} onClick={() => void act(false)}>{pending ? "Sending…" : "Resend confirmation email"}</Button>}
      {me && !done && <button className="ml-5 text-sm text-slate-500 underline" onClick={() => void logout()}>Sign out</button>}
    </section>
  </main>;
}
