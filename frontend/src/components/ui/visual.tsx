"use client";

import { useState } from "react";
import { Icon } from "./icon";

function ImageSurface({ src, alt }: { src: string | null; alt: string }) {
  const [failed, setFailed] = useState(false);
  if (!src || failed) return <div className="image-placeholder"><Icon name="archive" /><span>התמונה אינה זמינה</span></div>;
  // The browser loads the original asset directly, including when Next image optimization is unavailable.
  return <img src={src} alt={alt} onError={() => setFailed(true)} referrerPolicy="no-referrer" />;
}

export function Visual({
  front, back, title, className = "",
}: { front: string | null; back?: string | null; title: string; className?: string }) {
  const [reverse, setReverse] = useState(false);
  const selected = reverse && back ? back : front;
  return (
    <figure className={`visual ${className}`}>
      <ImageSurface key={selected ?? "missing"} src={selected} alt={reverse ? `גב התמונה — ${title}` : title} />
      {back && <button type="button" className="flip-button" aria-pressed={reverse}
        onClick={() => setReverse((value) => !value)}>{reverse ? "הצגת החזית" : "הצגת הגב"}</button>}
    </figure>
  );
}
