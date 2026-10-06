import path from "node:path";
import Module from "node:module";
import esbuild from "esbuild";

// Bundle browser import metadata so Node cache tests can load the real DataFrame and Store modules
export function loadDataFrame() {
  const filename = path.resolve(__dirname, "../../client/dataframe.ts");
  const built = esbuild.buildSync({
    entryPoints: [filename],
    bundle: true,
    platform: "node",
    format: "cjs",
    write: false,
    sourcemap: process.env.PRET_COVERAGE_DIR ? "inline" : false,
    treeShaking: process.env.PRET_COVERAGE_DIR ? false : undefined,
    define: { "import.meta.url": JSON.stringify(`file://${filename}`) },
    external: ["react", "use-sync-external-store/shim"],
  });
  const loaded = new Module(filename, module);
  loaded.filename = filename;
  loaded.paths = module.paths;
  loaded._compile(built.outputFiles[0].text, filename);
  return loaded.exports;
}
