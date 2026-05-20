/** @type {import('tailwindcss').Config} */
export default {
  content: [
    "./index.html",
    "./App.tsx",
    "./index.tsx",
    "./types.ts",
    "./components/**/*.{js,ts,jsx,tsx}",
    "./src/**/*.{js,ts,jsx,tsx}",
    "./stores/**/*.{js,ts,jsx,tsx}",
    "./services/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        ide: {
          bg: '#000000',        // Pure black background (outer frame)
          panel: '#1A1A1A',     // Lighter panel surfaces (visible against black)
          sidebar: '#111111',   // Very dark sidebar
          border: '#2A2A2A',    // Subtle dark borders (slightly lighter for visibility)
          accent: '#FF6B00',    // Vibrant orange (Figma-style)
          text: '#A1A1A1',      // Muted gray text
          textLight: '#FFFFFF', // White text
        }
      },
      borderRadius: {
        'sl-1': 'var(--sl-level1)',
        'sl-2': 'var(--sl-level2)',
        'sl-3': 'var(--sl-level3)',
      },
      spacing: {
        'sl-inset': 'var(--sl-inset)',
        'sl-gap': 'var(--sl-gap)',
      },
      fontFamily: {
        mono: ['ui-monospace', 'SFMono-Regular', 'Menlo', 'Monaco', 'Consolas', "Liberation Mono", "Courier New", 'monospace'],
      }
    }
  },
  plugins: [require('@tailwindcss/typography')],
}
