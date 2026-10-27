import typography from "@tailwindcss/typography";
/** @type {import('tailwindcss').Config} */
const token = (name) => `rgb(var(--${name}) / <alpha-value>)`;

export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  darkMode: ["selector", ':root:not([data-theme="light"])'],
  theme: {
    extend: {
      colors: {
        bg: token("bg"),
        panel: token("panel"),
        raised: token("raised"),
        line: token("line"),
        ink: token("ink"),
        muted: token("muted"),
        faint: token("faint"),
        brand: token("brand"),
        flame: token("flame"),
        ok: token("ok"),
        warn: token("warn"),
        bad: token("bad"),
        info: token("info"),
        violet: token("violet"),
      },
      fontFamily: {
        sans: ['"Inter Variable"', "ui-sans-serif", "system-ui", "sans-serif"],
        mono: ['"JetBrains Mono Variable"', "ui-monospace", "SFMono-Regular", "monospace"],
      },
      boxShadow: {
        card: "0 1px 0 0 rgb(var(--line) / 0.6), 0 8px 24px -12px rgb(0 0 0 / 0.35)",
        glow: "0 0 0 1px rgb(var(--brand) / 0.35), 0 0 24px -4px rgb(var(--brand) / 0.35)",
      },
      keyframes: {
        pulseDot: { "0%,100%": { opacity: "0.25" }, "50%": { opacity: "1" } },
        rise: { from: { opacity: "0", transform: "translateY(4px)" }, to: { opacity: "1", transform: "none" } },
      },
      animation: { pulseDot: "pulseDot 1.2s ease-in-out infinite", rise: "rise 160ms ease-out" },
    },
  },
  plugins: [typography],
};
