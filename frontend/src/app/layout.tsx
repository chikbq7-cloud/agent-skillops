import type { Metadata } from "next";
import "./globals.css";
import ThemeToggle from "@/components/ThemeToggle";

export const metadata: Metadata = {
  title: "Agent SkillOps",
  description: "Skill 优化、回归验证与人工发布工作台",
};

export default function RootLayout({
  children,
}: {
  children: React.ReactNode;
}) {
  return (
    <html lang="zh-CN" suppressHydrationWarning>
      <head>
        <script
          dangerouslySetInnerHTML={{
            __html: `
            (function() {
              var theme = localStorage.getItem('theme');
              if (theme === 'light') {
                document.documentElement.classList.add('light');
                document.documentElement.classList.remove('dark');
              } else {
                document.documentElement.classList.add('dark');
                document.documentElement.classList.remove('light');
              }
            })();
          `,
          }}
        />
      </head>
      <body>
        <ThemeToggle />
        {children}
      </body>
    </html>
  );
}
