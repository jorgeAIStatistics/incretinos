import type { NextConfig } from "next";

// The browser always talks to the same origin; Next proxies /api to the FastAPI service.
const API_URL = process.env.INCRETINOS_API_URL ?? "http://127.0.0.1:8000";

const isDevelopment = process.env.NODE_ENV === "development";

// Content-Security-Policy. The application loads nothing from another origin: its scripts, styles, fonts and
// images are its own, and the browser only calls the same-origin /api proxy. So everything is limited to 'self',
// plugins are refused, the page cannot be framed, and forms and <base> cannot point elsewhere.
//
// 'unsafe-inline' remains for scripts and styles because Next.js writes its bootstrap data as inline scripts and
// the charts use inline styles; removing it needs a per-request nonce (middleware and dynamic rendering of every
// page), which is a change to the application, not to this file. What the policy still stops: scripts, frames,
// objects and form posts from or to any other origin, and requests to any other origin.
//
// `next dev` additionally needs eval (React refresh and source maps) and its hot-reload websocket.
const contentSecurityPolicy = [
  "default-src 'self'",
  `script-src 'self' 'unsafe-inline'${isDevelopment ? " 'unsafe-eval'" : ""}`,
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data: blob:",
  "font-src 'self' data:",
  `connect-src 'self'${isDevelopment ? " ws: wss:" : ""}`,
  "worker-src 'self' blob:",
  "object-src 'none'",
  "base-uri 'self'",
  "form-action 'self'",
  "frame-ancestors 'none'",
].join("; ");

const nextConfig: NextConfig = {
  output: "standalone",
  // Browser tests set NEXT_DIST_DIR so their dev server can run next to a developer's own `next dev`.
  distDir: process.env.NEXT_DIST_DIR || ".next",
  async rewrites() {
    return [{ source: "/api/:path*", destination: `${API_URL}/api/:path*` }];
  },
  async headers() {
    return [
      {
        source: "/:path*",
        headers: [
          { key: "Content-Security-Policy", value: contentSecurityPolicy },
          { key: "X-Content-Type-Options", value: "nosniff" },
          { key: "X-Frame-Options", value: "DENY" },
          { key: "Referrer-Policy", value: "strict-origin-when-cross-origin" },
          { key: "Cross-Origin-Opener-Policy", value: "same-origin" },
          { key: "Permissions-Policy", value: "camera=(), microphone=(), geolocation=()" },
        ],
      },
    ];
  },
};

export default nextConfig;
