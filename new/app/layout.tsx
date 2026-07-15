import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "AI Elite BEMS Next",
  description: "사내 에너지 통합 관리 시스템",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="ko">
      <body>{children}</body>
    </html>
  );
}
