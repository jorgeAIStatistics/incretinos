"use client";

// Last resort: an error in the root layout itself. It cannot use the shell or the stylesheet, so it is plain HTML.
export default function GlobalError({ reset }: { error: Error & { digest?: string }; reset: () => void }) {
  return (
    <html lang="en">
      <body style={{ fontFamily: "system-ui, sans-serif", padding: "2rem", color: "#101820", background: "#f6f7f8" }}>
        <h1 style={{ fontSize: "1.25rem" }}>IncretinOS could not start</h1>
        <p>An unexpected error stopped the application from rendering. No evidence was changed.</p>
        <button type="button" onClick={reset} style={{ padding: "0.5rem 0.9rem", border: "1px solid #858d98", borderRadius: 6, background: "#fff", cursor: "pointer" }}>
          Try again
        </button>
      </body>
    </html>
  );
}
