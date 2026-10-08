import type { Metadata, Viewport } from "next";
import { Inter } from "next/font/google";
import { Dock } from "@/components/Dock";
import { TerminalProvider } from "@/components/TerminalProvider";
import { TopBar } from "@/components/TopBar";
import { CHAIN_ORDER } from "@/lib/chains";
import "./globals.css";

const inter = Inter({ subsets: ["latin"], weight: ["400", "500", "600", "700"], variable: "--font-inter", display: "swap" });

export const SITE_URL = process.env.NEXT_PUBLIC_SITE_URL ?? "http://localhost:3000";

export const metadata: Metadata = {
  metadataBase: new URL(SITE_URL),
  title: { default: "Bait", template: "%s · Bait" },
  description: "A terminal for Uniswap v4 hooks. See what's moving, who's trading it, why it matters, and swap.",
  alternates: { types: { "application/rss+xml": "/feed.xml" } },
  openGraph: { siteName: "Bait", type: "website" },
};

export const viewport: Viewport = { themeColor: "#0a0a13", colorScheme: "dark" };

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en" className={inter.variable}>
      <body>
        <TerminalProvider>
          <TopBar chains={CHAIN_ORDER} />
          {children}
          <Dock />
        </TerminalProvider>
      </body>
    </html>
  );
}
