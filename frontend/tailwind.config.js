/** @type {import('tailwindcss').Config} */
export default {
  darkMode: ["class"],
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      /* RADIUS. Tailwind's stock scale is 2/4/6/8/12/16px - the squared corners the
       * approved design does not have, and the reason `rounded-md` was drawing a 6px box
       * on a mockup whose smallest radius is 10px. Every name is re-pointed at the console
       * radius scale declared on `:root` in
       * src/components/conversations/consoleTheme.css (imported by App.tsx), so the scale
       * has exactly one home and `rounded-md` in any file lands on it for free.
       *
       * THERE WAS NO PARALLEL SYSTEM TO CLASH WITH: this config had no `borderRadius` key
       * and no `--radius` token anywhere in the tree before this block.
       *
       * The literal fallbacks are load-bearing. A `var()` that resolves to nothing makes
       * `border-radius` invalid, and an invalid border-radius is a SQUARE - so a page that
       * somehow renders without consoleTheme.css degrades to the right corners rather than
       * to the wrong ones. */
      borderRadius: {
        none: "0px",
        sm: "var(--cx-r-xs, 10px)",
        DEFAULT: "var(--cx-r-xs, 10px)",
        md: "var(--cx-r-sm, 12px)",
        lg: "var(--cx-r-md, 14px)",
        xl: "var(--cx-r-lg, 18px)",
        "2xl": "var(--cx-r-lg, 18px)",
        "3xl": "var(--cx-r-lg, 18px)",
        full: "var(--cx-r-pill, 999px)",
      },
      colors: {
        border: "hsl(var(--border))",
        background: "hsl(var(--background))",
        foreground: "hsl(var(--foreground))",
        muted: "hsl(var(--muted))",
        "muted-foreground": "hsl(var(--muted-foreground))",
        primary: "hsl(var(--primary))",
        "primary-foreground": "hsl(var(--primary-foreground))",
        destructive: "hsl(var(--destructive))",
        ambient: "hsl(var(--ambient))",
      },
    },
  },
  plugins: [],
};
