/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // Instrument panel enamel, not near-black. The slight blue cast is
        // what keeps a dark UI from reading as a void.
        // Every colour is a CSS variable so a second theme is one override
        // block, not a rewrite. The hex values live in index.css under :root
        // (dark) and .theme-light.
        panel: {
          DEFAULT: "rgb(var(--panel) / <alpha-value>)",
          raised: "rgb(var(--panel-raised) / <alpha-value>)",
          high: "rgb(var(--panel-high) / <alpha-value>)",
        },
        line: {
          DEFAULT: "rgb(var(--line) / <alpha-value>)",
          bright: "rgb(var(--line-bright) / <alpha-value>)",
        },
        ink: {
          DEFAULT: "rgb(var(--ink) / <alpha-value>)",
          dim: "rgb(var(--ink-dim) / <alpha-value>)",
          faint: "rgb(var(--ink-faint) / <alpha-value>)",
        },
        // Semantic signal set. On a real switchboard, colour means state --
        // so these map to connector status, never to decoration.
        signal: {
          live: "#22D3A5",   // Charging: current flowing
          hold: "#F4A93C",   // SuspendedEVSE: we are holding it at 0 W
          idle: "#6B7A99",   // Available / Preparing
          fault: "#FF5C5C",  // Faulted
          wait: "#7C9CF5",   // Waiting for a Start command
        },
      },
      fontFamily: {
        sans: ["Space Grotesk", "system-ui", "sans-serif"],
        mono: ["JetBrains Mono", "ui-monospace", "monospace"],
      },
      fontSize: {
        eyebrow: ["0.6875rem", { lineHeight: "1", letterSpacing: "0.14em" }],
      },
      boxShadow: {
        panel: "0 1px 0 0 rgba(255,255,255,0.04) inset, 0 8px 24px -12px rgba(0,0,0,0.8)",
        glow: "0 0 0 1px currentColor, 0 0 18px -4px currentColor",
      },
      keyframes: {
        pulse_pip: {
          "0%, 100%": { opacity: "1", transform: "scale(1)" },
          "50%": { opacity: "0.45", transform: "scale(0.82)" },
        },
        sweep: {
          "0%": { transform: "translateX(-100%)" },
          "100%": { transform: "translateX(300%)" },
        },
        rise: {
          from: { opacity: "0", transform: "translateY(6px)" },
          to: { opacity: "1", transform: "translateY(0)" },
        },
      },
      animation: {
        pip: "pulse_pip 2s cubic-bezier(0.4,0,0.6,1) infinite",
        sweep: "sweep 2.4s linear infinite",
        rise: "rise 0.28s ease-out both",
      },
    },
  },
  plugins: [],
};