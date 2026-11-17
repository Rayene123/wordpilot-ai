import "./globals.css";

export const metadata = {
  title: "WordPilot | Document workspace",
  description: "A controlled workflow for reviewing and verifying AI-assisted document edits.",
};

export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return <html lang="en"><body>{children}</body></html>;
}
