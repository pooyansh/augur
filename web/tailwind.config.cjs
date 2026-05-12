/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    "./index.html",
    "./src/**/*.{js,ts,jsx,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        // Brand palette for status indicators
        alive: "#22c55e",   // green-500
        dead: "#ef4444",    // red-500
        paper: "#6366f1",   // indigo-500
        live: "#f59e0b",    // amber-500
      },
    },
  },
  plugins: [],
};
