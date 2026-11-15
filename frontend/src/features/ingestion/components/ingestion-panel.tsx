"use client";

import { useEffect, useRef, useState } from "react";
import { api } from "@/lib/api/client";
import { errorText } from "@/lib/api/errors";
import type { Action, JobSnapshot, PhotoProgress } from "@/lib/api/types";
import { Visual } from "@/components/ui/visual";

const stages = [
  { key: "vision", label: "ניתוח התמונה (Vision)" },
  { key: "vision_embedding", label: "יצירת וקטור תמונה" },
  { key: "catalog", label: "יצירת וקטור קטלוג" },
  { key: "save", label: "שמירה במאגר" },
];
const statuses: Record<JobSnapshot["status"], string> = {
  queued: "מתחילים בעיבוד האוסף…", running: "מעבד כעת", pausing: "משהים בסיום השלב הנוכחי…",
  paused: "העיבוד מושהה", restarting: "ממתינים לסיום השלב ומתחילים מחדש…",
  completed: "עיבוד האוסף הושלם", completed_with_errors: "העיבוד הסתיים עם שגיאות",
};

const stageStatuses: Record<string, string> = {
  completed: "הושלם", running: "בעיבוד", failed: "נכשל", pending: "ממתין",
};

function usePhotoProgress(job: JobSnapshot | null) {
  const seen = useRef<{ jobId: number; savedId?: number } | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [preview, setPreview] = useState<{ jobId: number; photo: PhotoProgress } | null>(null);
  const [now, setNow] = useState(() => Date.now());

  useEffect(() => {
    if (!job) return;
    const saved = job.last_completed;
    const previous = seen.current;
    seen.current = { jobId: job.id, savedId: saved?.save_task_id };
    if (!previous || previous.jobId !== job.id || (job.status !== "running" && job.status !== "queued")) {
      if (timer.current) clearTimeout(timer.current);
      setPreview(null);
      return;
    }
    if (saved && saved.save_task_id !== previous.savedId && saved.stages.every((stage) => stage.status === "completed")) {
      if (timer.current) clearTimeout(timer.current);
      // Show the real saved image with its completed checklist, even when save took
      // less than one poll. The worker continues processing the next image.
      setPreview({ jobId: job.id, photo: saved });
      timer.current = setTimeout(() => setPreview(null), 2400);
    }
  }, [job]);

  useEffect(() => () => { if (timer.current) clearTimeout(timer.current); }, []);

  const showingCompletion = preview?.jobId === job?.id && (job?.status === "running" || job?.status === "queued");
  const photo = showingCompletion ? preview!.photo : job?.current;
  const startedAt = photo?.stages.find((stage) => stage.status === "running")?.started_at;
  useEffect(() => {
    if (!startedAt) return;
    setNow(Date.now());
    const clock = setInterval(() => setNow(Date.now()), 1000);
    return () => clearInterval(clock);
  }, [startedAt]);
  const seconds = startedAt ? Math.max(0, Math.floor((now - Date.parse(startedAt)) / 1000)) : 0;
  const elapsed = Number.isFinite(seconds) ? `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}` : "";
  return { photo, showingCompletion, elapsed };
}

export function ProcessIcon({ name }: { name: "layers" | "restart" | "pause" | "play" | "activity" }) {
  return <svg className={`process-icon process-icon-${name}`} width="24" height="24" viewBox="0 0 24 24" fill="none"
    stroke="currentColor" strokeWidth="1.8" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    {name === "layers" && <><path d="m12 3 9 5-9 5-9-5 9-5Z" /><path d="m3 12 9 5 9-5M3 16l9 5 9-5" /></>}
    {name === "restart" && <><path d="M20 7a9 9 0 0 0-15-2L3 8m0-5v5h5M4 17a9 9 0 0 0 15 2l2-3m0 5v-5h-5" /></>}
    {name === "pause" && <><path d="M8 5v14M16 5v14" strokeWidth="4" /></>}
    {name === "play" && <path d="m8 4 12 8-12 8V4Z" fill="currentColor" stroke="none" />}
    {name === "activity" && <path d="M2 12h5l3-9 4 18 3-9h5" />}
  </svg>;
}

export function Ingestion({ ready }: { ready: boolean }) {
  const [job, setJob] = useState<JobSnapshot | null>(null);
  const [error, setError] = useState("");
  const [connectionError, setConnectionError] = useState("");
  const [busy, setBusy] = useState(false);
  const dialog = useRef<HTMLDialogElement>(null);
  const sequence = useRef(0);
  const acting = useRef(false);

  useEffect(() => {
    let active = true;
    let timer: number;
    async function update() {
      if (!acting.current) {
        const request = ++sequence.current;
        try {
          const snapshot = await api<JobSnapshot>("ingestion");
          if (active && request === sequence.current) {
            setJob(snapshot);
            setConnectionError("");
          }
        } catch (failure) {
          if (active && request === sequence.current) setConnectionError(errorText(failure instanceof Error ? failure.message : "api_unavailable"));
        }
      }
      if (active) timer = window.setTimeout(update, 1200);
    }
    void update();
    return () => { active = false; window.clearTimeout(timer); };
  }, []);

  async function act(action: Action) {
    if (!job || acting.current) return;
    let acknowledge = false;
    if (action === "resume" && job.access_blocked) {
      acknowledge = window.confirm("אישור שהגישה למקור הוסדרה ואפשר לחדש את הבקשות?");
      if (!acknowledge) return;
    }
    acting.current = true;
    ++sequence.current;
    setBusy(true);
    setError("");
    try {
      setJob(await api<JobSnapshot>(`ingestion/${action}`, {
        job_id: job.id, acknowledge_access: acknowledge, confirm_erase: action === "restart",
      }));
      dialog.current?.close();
    } catch (failure) {
      setError(errorText(failure instanceof Error ? failure.message : "job_transition"));
    } finally {
      acting.current = false;
      setBusy(false);
    }
  }

  const overall = job?.collection_progress;
  const processing = job?.status === "queued" || job?.status === "running";
  const waiting = job?.status === "pausing" || job?.status === "restarting";
  const completed = job?.status === "completed";
  const { photo, showingCompletion, elapsed } = usePhotoProgress(job);

  return <aside id="ingestion" className="panel ingestion-panel" aria-labelledby="ingestion-heading">
    <header className="ingestion-header">
      <span className="section-icon"><ProcessIcon name="layers" /></span>
      <h2 id="ingestion-heading">עיבוד תמונות מתוך מאגר יד ושם</h2>
      <button type="button" className="restart-button" disabled={busy || !job || waiting || !ready}
        onClick={() => dialog.current?.showModal()}><ProcessIcon name="restart" />ביצוע מחדש</button>
    </header>
    <div className="ingestion-body">
      <section className="collection-progress" aria-label="התקדמות עיבוד האוסף">
        <div className="progress-summary">
          <span className="progress-fraction" dir="ltr" aria-label="רשומות שהושלמו מתוך סך האוסף">
            {overall?.completed_records.toLocaleString("he-IL") ?? "—"}<span>/</span>{overall?.total_records?.toLocaleString("he-IL") ?? "—"}
          </span>
          <strong dir="ltr">{overall?.percent == null ? "—" : `${(Math.floor(overall.percent * 10) / 10).toLocaleString("he-IL", { maximumFractionDigits: 1 })}%`}</strong>
        </div>
        <progress aria-label="התקדמות עיבוד האוסף" value={overall?.percent ?? 0} max={100} />
      </section>
      <div className="ingestion-actions">
        {!completed && <button type="button" className="primary-button pause-button"
          disabled={busy || !job || waiting || (!processing && !ready)}
          onClick={() => void act(processing ? "pause" : "resume")}>
          <ProcessIcon name={processing || job?.status === "pausing" ? "pause" : "play"} />
          {waiting ? (job?.status === "restarting" ? "מתחילים מחדש…" : "משהים…") : processing ? "השהיה" : "המשך"}
        </button>}
        <span className="process-status" role="status">{job ? (job.status === "running" ? "" : statuses[job.status]) : "טוענים את תהליך העיבוד…"}</span>
      </div>
      {!ready && job?.status === "paused" && <p className="process-note">ממתינים לחיבור למודלים המקומיים כדי להמשיך בעיבוד.</p>}
      {error && <p className="inline-error" role="alert">{error}</p>}
      {connectionError && <p className="inline-error" role="alert">{connectionError}</p>}
      {job?.error_code && <p className="inline-error" role="status">{errorText(job.error_code)}{job.failed_records > 0 && ` · ${job.failed_records} פריטים לא הושלמו`}</p>}
      <section className="current-photo" aria-labelledby="current-heading">
        <div className="photo-stages">
          <h3 id="current-heading"><ProcessIcon name="activity" />{showingCompletion ? "התמונה נשמרה במאגר" : job && !processing ? statuses[job.status] : "מעבד כעת"}</h3>
          {photo && <p className="current-photo-caption">תמונה <bdi>{photo.photo_id}</bdi></p>}
          <ol aria-label="שלבי עיבוד התמונה">
            {stages.map((stage) => {
              const progress = photo?.stages.find((item) => item.key === stage.key);
              const state = progress?.status ?? "pending";
              return <li key={stage.key} className={`stage stage-${state}`} aria-current={state === "running" ? "step" : undefined}>
                <span className="stage-indicator" aria-hidden="true">{state === "completed" ? "✓" : state === "failed" ? "!" : ""}</span>
                <span className="stage-copy"><span>{stage.label}</span><small>{state === "completed" && progress?.reused ? "כבר קיים במאגר" : stageStatuses[state] ?? "ממתין"}
                  {state === "running" && processing && elapsed && <> · <bdi>{elapsed}</bdi></>}
                </small></span>
              </li>;
            })}
          </ol>
        </div>
        <div className="current-photo-image">
          {photo ? <Visual key={`${photo.photo_id}:${photo.image_id}`} front={photo.image_url}
            back={photo.back_url} title={photo.title ?? `תמונה ${photo.photo_id}`} />
            : <div className="photo-empty"><ProcessIcon name="layers" /><p>{job?.activity === "count" ? "בודקים את גודל האוסף…" : job?.activity === "discovery" ? "מאתרים תמונות מהאוסף…" : "התמונה שבעיבוד תופיע כאן"}</p></div>}
        </div>
      </section>
      {job?.last_completed && <p className="last-completed" role="status">✓ התמונה האחרונה שנשמרה במאגר: <bdi>{job.last_completed.photo_id}</bdi></p>}
    </div>
    <dialog ref={dialog} className="restart-dialog" aria-labelledby="restart-title" aria-describedby="restart-description"
      onCancel={(event) => { if (busy) event.preventDefault(); }}>
      <div className="dialog-symbol"><ProcessIcon name="restart" /></div>
      <h2 id="restart-title">לבצע את העיבוד מחדש?</h2>
      <p id="restart-description">פעולה זו תמחק את נתוני העיבוד הקודמים — תיאורי התמונות, המידע שהופק והווקטורים — ותתחיל עיבוד חדש של כל האוסף. לא ניתן לבטל את המחיקה.</p>
      {error && <p className="inline-error" role="alert">{error}</p>}
      <div className="dialog-actions">
        <button type="button" className="secondary-button" disabled={busy} onClick={() => dialog.current?.close()} autoFocus>ביטול</button>
        <button type="button" className="danger-button" disabled={busy} onClick={() => void act("restart")}>{busy ? "מתחילים מחדש…" : "מחיקה וביצוע מחדש"}</button>
      </div>
    </dialog>
  </aside>;
}
