// The build for the one React island. Node is a build-time tool only: this
// produces a hashed bundle and a manifest under static/board/, both of which
// collectstatic picks up like any other static file, and the running
// application never needs Node again.
//
// There is one entry point and one mount, by decision — see the Constraints of
// issue #13. A second island is a new conversation, not another line here.
//
// No framework plugin: esbuild compiles .jsx on its own, and the automatic JSX
// runtime below is what lets a component file contain no `import React`. Hot
// module replacement and the Vite dev server are out of scope (#39), so there
// is one build path in development and in production.
import { defineConfig } from "vite";

export default defineConfig({
  build: {
    // Inside static/, so the existing STATICFILES_DIRS entry finds it and the
    // Docker image that copies static/ ships it. Git-ignored: it is generated.
    outDir: "static/board",
    emptyOutDir: true,
    // Written as static/board/manifest.json rather than the default hidden
    // .vite/ directory, because collectstatic skips dot-directories and the
    // manifest has to be collected with the bundle it names.
    manifest: "manifest.json",
    rollupOptions: {
      input: "assets/js/board.jsx",
    },
  },
  esbuild: {
    jsx: "automatic",
  },
});
