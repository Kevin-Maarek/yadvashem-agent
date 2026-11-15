import type { ImageResult } from "@/lib/api/types";
import { Visual } from "@/components/ui/visual";

const metadataLabels: Record<string, string> = {
  signature: "סימול ארכיוני", date: "תאריך ברשומת המקור", source: "מקור", credit: "קרדיט",
  submitter: "מוסר הפריט", materialType: "סוג חומר", relatedTitle: "אוסף קשור",
  places: "מקומות", names: "שמות", year_start: "משנה", year_end: "עד שנה",
};

const rankLabels: Record<string, string> = {
  catalog: "דמיון קטלוגי", vision: "דמיון חזותי", lexical: "התאמת טקסט", exact: "התאמה מדויקת",
};

function metadataText(value: unknown): string | null {
  if (typeof value === "string" || typeof value === "number") return String(value);
  if (Array.isArray(value) && value.every((entry) => typeof entry === "string")) return value.join(" · ");
  return null;
}

export function ResultCard({ item, best }: { item: ImageResult; best: boolean }) {
  const metadata = Object.entries(item.metadata)
    .filter(([name]) => name in metadataLabels)
    .map(([name, value]) => [metadataLabels[name], metadataText(value)] as const)
    .filter(([, value]) => value);
  const ranked = Object.keys(item.ranks).length > 0;
  return (
    <article className={best ? "result-card result-best" : "result-card"}>
      <Visual key={item.image_id} front={item.image_url} back={item.back_url}
        title={item.title} />
      <div className="result-body">
        <div className="result-kicker">
          {best && <span className="best-label">תוצאה ראשונה</span>}
          <span>פריט <bdi>{item.photo_id}</bdi></span>
          {ranked && <span title="ציון ביחס לתוצאה הראשונה בשאילתה" className="relative-score">
            <bdi>{Math.round(item.relative_score * 100)}%</bdi> יחסי
          </span>}
        </div>
        <h4 dir="auto">{item.title}</h4>
        {item.catalog_description && <p className="catalog-description" dir="auto">{item.catalog_description}</p>}
        {!!item.subjects.length && <div className="subjects">
          {item.subjects.map((subject) => <span key={subject} dir="auto">{subject}</span>)}
        </div>}
        {!!metadata.length && <details className="result-details">
          <summary>פרטי הקטלוג</summary>
          <dl>{metadata.map(([label, value]) => <div key={label}><dt>{label}</dt><dd dir="auto">{value}</dd></div>)}</dl>
        </details>}
        {item.vision_description && <details className="result-details">
          <summary>תיאור חזותי שנוצר ב־AI</summary>
          <p dir="auto">{item.vision_description}</p>
        </details>}
        {ranked && <details className="result-details">
          <summary>איך דורגה התוצאה</summary>
          <ul>{Object.entries(item.ranks).map(([channel, position]) =>
            <li key={channel}>{rankLabels[channel] ?? channel} · מקום <bdi>{position}</bdi></li>)}</ul>
        </details>}
        {item.source_url && <a className="source-link" href={item.source_url}
          target="_blank" rel="noreferrer">לרשומה באוסף יד ושם <span aria-hidden="true">↗</span></a>}
      </div>
    </article>
  );
}
