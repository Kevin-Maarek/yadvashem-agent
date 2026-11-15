"use client";

import { FormEvent, useEffect, useRef, useState } from "react";
import { api } from "@/lib/api/client";
import { errorText } from "@/lib/api/errors";
import type { ChatResult, Config, Health } from "@/lib/api/types";
import { chatEvents } from "./api";
import { ResultCard } from "./components/result-card";
import { Ingestion } from "@/features/ingestion/components/ingestion-panel";

type Turn = { id: number; message: string; text: string; result: ChatResult | null; failed?: boolean };
const actionLabels: Record<string, string> = {
  search: "חיפוש באוסף", discuss: "שיחה על התוצאות", investigate: "מחקר במאגר השמות",
};

export default function Home() {
  const [health, setHealth] = useState<Health | null>(null);
  const [limit, setLimit] = useState(5);
  const [threshold, setThreshold] = useState(85);
  const [topOnly, setTopOnly] = useState(false);
  const [message, setMessage] = useState("");
  const [turns, setTurns] = useState<Turn[]>([]);
  const [busy, setBusy] = useState(false);
  const [connectionError, setConnectionError] = useState(false);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [creatingConversation, setCreatingConversation] = useState(true);
  const [chatError, setChatError] = useState("");
  const input = useRef<HTMLTextAreaElement>(null);
  const latest = useRef<HTMLDivElement>(null);
  const firstTurn = useRef(true);
  const loadedConfig = useRef(false);

  async function startConversation() {
    setCreatingConversation(true);
    setChatError("");
    try {
      const conversation = await api<{ id: string }>("conversations", {});
      setConversationId(conversation.id);
      setTurns([]);
      setMessage("");
      firstTurn.current = true;
    } catch (failure) {
      setChatError(errorText(failure instanceof Error ? failure.message : "chat_error"));
    } finally { setCreatingConversation(false); }
  }

  useEffect(() => {
    void startConversation();
  }, []);

  useEffect(() => {
    let active = true;
    async function load() {
      try {
        const [settings, models] = await Promise.all([
          api<Config>("config"), api<Health>("health/ai"),
        ]);
        if (!active) return;
        setHealth(models);
        setConnectionError(false);
        if (!loadedConfig.current) {
          loadedConfig.current = true;
          setLimit(settings.search_limit);
          setThreshold(Math.round(settings.search_threshold * 100));
        }
      } catch {
        if (active) setConnectionError(true);
      }
    }
    void load();
    const timer = window.setInterval(load, 30000);
    return () => { active = false; window.clearInterval(timer); };
  }, []);

  async function submit(event: FormEvent) {
    event.preventDefault();
    const query = message.trim();
    if (busy || creatingConversation || !conversationId || !query) return;
    const id = Date.now();
    setBusy(true);
    setMessage("");
    setTurns((existing) => [...existing, { id, message: query, text: "", result: null }]);
    try {
      const result = await chatEvents({
        message: query, source: "live", limit, threshold: threshold / 100, top_only: topOnly,
        conversation_id: conversationId, request_id: crypto.randomUUID(),
      }, (delta) => setTurns((existing) => existing.map((turn) =>
        turn.id === id ? { ...turn, text: turn.text + delta } : turn)));
      setTurns((existing) => existing.map((turn) => turn.id === id ? { ...turn, result, text: result.text } : turn));
    } catch (failure) {
      const code = failure instanceof Error ? failure.message : "chat_error";
      setTurns((existing) => existing.map((turn) => turn.id === id ? { ...turn, text: errorText(code), failed: true } : turn));
    } finally {
      setBusy(false);
    }
  }

  useEffect(() => {
    if (turns.length === 0 || busy) return;
    if (firstTurn.current) {
      firstTurn.current = false;
      return;
    }
    if (window.matchMedia("(min-width: 760px)").matches) {
      latest.current?.scrollIntoView({ block: "nearest", behavior: "smooth" });
    }
  }, [busy, turns.length]);

  const ready = health?.ready && !connectionError;

  return (
    <>
      <a href="#search" className="skip-link">דילוג לחיפוש</a>
      <header className="site-header shell">
        <a href="/" className="brand" aria-label="יד ושם — חזרה לעמוד הראשי">
          <svg width="38" height="48" viewBox="0 0 38 48" fill="none" aria-hidden="true">
            <path d="M19 3v22M11 5l1 18M4 9l3 19q12 8 24-1l3-18M27 5l-1 18M8 22q11 8 22-1M9 32q10 6 20 0M19 35v9M12 44h14" stroke="currentColor" strokeWidth="3.2" />
          </svg>
          <span>יד ושם</span>
        </a>
      </header>
      <main className="shell main-content">
        <nav className="mobile-jump"><a href="#search">חיפוש באוסף</a><a href="#ingestion">קליטת תמונות ↓</a></nav>
        <div className="workspace">
          <section className="panel search-panel" id="search" aria-labelledby="search-heading">
            <header className="chat-banner">
              <div className="archive-frames" aria-hidden="true">
                <span><img alt="" src="https://assets.yadvashem.org/image/upload/t_f_low_image/f_auto/q_auto/v1/remote_media/arch_srika/9501-10000/9501-9750/9701_4.jpg" referrerPolicy="no-referrer" /></span>
                <span><img alt="" src="https://assets.yadvashem.org/image/upload/t_f_low_image/f_auto/q_auto/v1/remote_media/arch_srika/6001-6500/6173-6325/6256_3.jpg" referrerPolicy="no-referrer" /></span>
              </div>
              <h2 id="search-heading">סוכן חכם אל תוך העבר</h2>
            </header>
            <div className="panel-body search-body">
              {turns.length > 0 && <div className="chat-controls">
                <button type="button" disabled={busy || creatingConversation} onClick={() => void startConversation()}>שיחה חדשה</button>
              </div>}
              {turns.length === 0 && <div className="welcome-message"><strong>שלום! 👋</strong><p>שאלו אותי על תמונות, אנשים, מקומות ואירועים מתוך מאגר יד ושם.</p></div>}
              {creatingConversation && <p role="status">פותחים שיחה חדשה…</p>}
              {chatError && <p className="inline-error" role="alert">{chatError}</p>}
              {turns.length > 0 && <div className="conversation" aria-live="polite">
                {turns.map((turn, index) => <div key={turn.id} className="turn"
                  ref={index === turns.length - 1 ? latest : undefined}>
                  <div className="user-message" dir="auto">{turn.message}</div>
                  {turn.result && actionLabels[turn.result.action] && <p className="chat-action">{actionLabels[turn.result.action]}</p>}
                  {turn.text ? <p className={`answer ${turn.failed ? "inline-error" : ""}`} dir="auto">{turn.text}</p>
                    : <p className="searching"><span className="pulse" />חושב...</p>}
                  {turn.result?.images.length ? <div className="results">
                    <ResultCard item={turn.result.images[0]} best />
                    {turn.result.images.length > 1 && <div className="additional-results">
                      <h3>תמונות נוספות</h3>
                      {turn.result.images.slice(1).map((image) => <ResultCard key={image.image_id} item={image} best={false} />)}
                    </div>}
                  </div> : null}
                  {!!turn.result?.citations.length && <div className="citation-links">
                    {turn.result.citations.map((citation) => <a className="source-link" key={citation.photo_id}
                      href={citation.url} target="_blank" rel="noreferrer">{citation.title} ↗</a>)}
                  </div>}
                  {!!turn.result?.research_citations?.length && <div className="citation-links" aria-label="מקורות ממאגר השמות">
                    {turn.result.research_citations.map((citation) => <a className="source-link" key={citation.source_id}
                      href={citation.url} target="_blank" rel="noreferrer" dir="auto">{citation.title} ↗</a>)}
                  </div>}
                </div>)}
              </div>}
              <form onSubmit={submit} className="composer">
                <label className="sr-only" htmlFor="query">הודעה לארכיון</label>
                <textarea id="query" ref={input} value={message} onChange={(event) => setMessage(event.target.value)}
                  rows={1} maxLength={1200} dir="auto" placeholder="כתבו כאן את שאלתכם..."
                  onKeyDown={(event) => {
                    if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
                      event.preventDefault();
                      event.currentTarget.form?.requestSubmit();
                    }
                  }} />
                <div className="composer-bar">
                  <details className="settings">
                    <summary>אפשרויות תוצאות</summary>
                    <div className="search-settings">
                      <label>מספר תמונות מרבי
                        <input type="number" dir="ltr" min={1} max={15} value={limit} disabled={topOnly}
                          onChange={(event) => setLimit(Math.max(1, Math.min(15, Number(event.target.value) || 1)))} />
                      </label>
                      <label>סף ביחס לתוצאה הראשונה <bdi>{threshold}%</bdi>
                        <input type="range" min={0} max={100} value={threshold} disabled={topOnly}
                          onChange={(event) => setThreshold(Number(event.target.value))} />
                      </label>
                      <label className="check-line"><input type="checkbox" checked={topOnly}
                        onChange={(event) => setTopOnly(event.target.checked)} /> הצגת התוצאה הראשונה בלבד</label>
                    </div>
                  </details>
                  <button className="primary-button" type="submit" disabled={!message.trim() || busy || creatingConversation || !conversationId}>
                    <span className="sr-only">{busy ? "חושבים…" : "שליחה"}</span>
                    <svg width="26" height="26" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.7" strokeLinejoin="round" aria-hidden="true"><path d="m21 3-6 18-4-8-8-4 18-6ZM11 13 21 3" /></svg>
                  </button>
                </div>
              </form>
            </div>
          </section>
          <Ingestion ready={Boolean(ready)} />
        </div>
        <footer className="footer">
          <span>התמונות מקושרות לרשומות המקור של יד ושם.</span>
          <span>יישום עצמאי · ללא שיוך רשמי ליד ושם</span>
        </footer>
      </main>
    </>
  );
}
