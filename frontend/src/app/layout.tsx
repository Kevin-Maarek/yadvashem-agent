import type { Metadata } from "next";
import "./styles.css";

export const metadata: Metadata = {
  title: "ארכיון חזותי | חיפוש באוסף",
  description: "חיפוש בתצלומים ובמסמכים ארכיוניים מאוסף יד ושם",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="he" dir="rtl">
      <body>{children}</body>
    </html>
  );
}
