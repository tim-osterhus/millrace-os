// Maintainer production build of the stock client; the dev server is unused.
// `scripts/export_dashboard.py` turns `dist/` into the packaged closure.
import { defineConfig, type Plugin } from "vite";

// Record which source modules each emitted chunk contains, so the exporter
// can attribute every emitted byte to first-party code, a locked package or
// a bundler helper (and refuse anything else).
function moduleClosure(): Plugin {
  return {
    name: "millrace-module-closure",
    apply: "build",
    generateBundle(_options, bundle) {
      const chunks: { fileName: string; modules: { id: string; renderedLength: number }[] }[] =
        [];
      for (const item of Object.values(bundle)) {
        if (item.type !== "chunk") continue;
        const modules = Object.entries(item.modules)
          .map(([id, rendered]) => ({ id, renderedLength: rendered.renderedLength }))
          .sort((a, b) => (a.id < b.id ? -1 : a.id > b.id ? 1 : 0));
        chunks.push({ fileName: item.fileName, modules });
      }
      chunks.sort((a, b) => (a.fileName < b.fileName ? -1 : 1));
      this.emitFile({
        type: "asset",
        fileName: "module-closure.json",
        source: JSON.stringify({ format: "millrace-os-module-closure/v1", chunks }, null, 1),
      });
    },
  };
}

export default defineConfig({
  base: "/ui/stock/",
  publicDir: false,
  plugins: [moduleClosure()],
  build: {
    outDir: "dist",
    emptyOutDir: true,
    manifest: true,
    sourcemap: false,
    // No data: URLs; every asset is a separate manifest-listed file.
    assetsInlineLimit: 0,
    // Supported browsers preload modules natively; no inline polyfill code.
    modulePreload: { polyfill: false },
    reportCompressedSize: false,
  },
});
