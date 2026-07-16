import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "AI Elite BEMS Next",
  description: "사내 에너지 통합 관리 시스템",
};

// 첫 페인트 전에 저장된 테마(없으면 OS 설정)를 <html data-theme>에 새겨
// 다크모드 사용자에게 밝은 화면이 번쩍이지 않도록 한다.
const themeInit = `try{var t=localStorage.getItem("bems-theme");if(t!=="dark"&&t!=="light"){t=matchMedia("(prefers-color-scheme: dark)").matches?"dark":"light"}document.documentElement.dataset.theme=t}catch(e){}`;

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="ko" suppressHydrationWarning>
      <body>
        <script dangerouslySetInnerHTML={{ __html: themeInit }} />
        {children}
      </body>
    </html>
  );
}
