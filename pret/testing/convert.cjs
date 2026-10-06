const fs = require("node:fs");
const path = require("node:path");
const { fileURLToPath } = require("node:url");
const { createHash } = require("node:crypto");
const { createInstrumenter } = require("istanbul-lib-instrument");
const { transformSync } = require("esbuild");
const v8ToIstanbul = require("v8-to-istanbul");
const { TraceMap, decodedMappings, originalPositionFor } = require("@jridgewell/trace-mapping");
const { GenMapping, addSegment, setSourceContent, toEncodedMap } = require("@jridgewell/gen-mapping");
const statementCache = new Map();

// Use Istanbul statement locations for the inventory without adding instrumentation to executed code
function statements(code) {
  const key = createHash("sha256").update(code).digest("hex");
  if (statementCache.has(key)) return statementCache.get(key);
  const options = { compact: false, produceSourceMap: false, autoWrap: true, esModules: false };
  let instrumenter = createInstrumenter(options);
  try {
    instrumenter.instrumentSync(code, "coverage-input.js");
  } catch (error) {
    if (!["ImportOutsideModule", "ImportMetaOutsideModule"].includes(error.reasonCode)) throw error;
    instrumenter = createInstrumenter({ ...options, esModules: true });
    instrumenter.instrumentSync(code, "coverage-input.js");
  }
  const result = Object.values(instrumenter.lastFileCoverage().statementMap);
  statementCache.set(key, result);
  return result;
}

// Keep the direct Transcrypt test path mapped to its original extracted Python function
function inlineTranscrypt(code, map, origin) {
  const output = new GenMapping();
  decodedMappings(new TraceMap(map)).forEach((segments, line) => {
    for (const segment of segments)
      if (segment.length > 1) {
        addSegment(output, line, segment[0], origin.path, segment[2] + origin.line - 1, 0);
      }
  });
  setSourceContent(output, origin.path, origin.source);
  return (
    code.replace(/\/\/# sourceMappingURL=[^\n]*/g, "") +
    "\n//# sourceMappingURL=data:application/json;base64," +
    Buffer.from(JSON.stringify(toEncodedMap(output))).toString("base64")
  );
}

// Build a map for the exact script seen by V8 including its new Function wrapper
function bundleMap(bundle, source) {
  const start = source.indexOf(bundle.code);
  if (start < 0) throw new Error(`Generated bundle differs from its coverage snapshot ${bundle.url}`);
  const prefix = source.slice(0, start).split("\n").length - 1;
  const locations = new Map();
  for (const module of bundle.modules) {
    decodedMappings(new TraceMap(module.map)).forEach((segments, line) => {
      const segment = segments.find((segment) => segment.length > 1);
      if (segment && module.python[segment[2] + 1]) {
        locations.set(prefix + module.line + line, module.python[segment[2] + 1]);
      }
    });
    for (const [line, origin] of Object.entries(module.javascript)) {
      locations.set(prefix + module.line + Number(line) - 1, origin);
    }
  }
  const map = new GenMapping();
  source.split("\n").forEach((_, line) => {
    const origin = locations.get(line);
    if (origin) addSegment(map, line, 0, origin[0], origin[1] - 1, 0);
    else addSegment(map, line, 0);
  });
  for (const [filename, snapshot] of Object.entries(bundle.sources)) setSourceContent(map, filename, snapshot.source);
  return toEncodedMap(map);
}

// Resolve source map paths against the inventory and reject stale assets with matching filenames
function sourcePaths(map, entry, inventory) {
  return map.sources.map((source, index) => {
    if (/(?:^|\/)(?:node_modules|submodules)\//.test(source)) return null;
    const content = map.sourcesContent?.[index];
    let clean = source.startsWith("file:") ? fileURLToPath(source) : source;
    const namespace = /^webpack:\/\/([^/]*)\//.exec(clean)?.[1];
    clean = clean.replace(/^webpack:\/\/[^/]*\//, "").replace(/^\.\//, "");
    if (path.isAbsolute(clean) && !inventory.files[clean]) return null;
    const local = entry.url.startsWith("file:") ? fileURLToPath(entry.url) : entry.url;
    const candidates = Object.keys(inventory.files).filter(
      (filename) =>
        filename === clean ||
        filename === path.resolve(path.dirname(local), clean) ||
        (filename.endsWith("/" + clean) &&
          (!inventory.roots[namespace] || filename.startsWith(inventory.roots[namespace] + "/")))
    );
    let filename = candidates.length === 1 ? candidates[0] : undefined;
    if (!filename && content !== undefined && content !== null) {
      const matches = Object.keys(inventory.files).filter((filename) => inventory.files[filename].source === content);
      if (matches.length === 1) filename = matches[0];
    }
    if (!filename) return null;
    if (content !== inventory.files[filename].source)
      throw new Error(`Stale or missing source content for ${filename}`);
    return filename;
  });
}

// Include untouched TS files and embedded JS bodies using the same statement inventory as executed scripts
function inventoryLines(inventory) {
  for (const [filename, file] of Object.entries(inventory.files)) {
    const lines = new Set(file.lines);
    if (file.language === "javascript") {
      const built = transformSync(file.source, {
        sourcefile: filename,
        loader: path.extname(filename).slice(1).replace("mjs", "js").replace("cjs", "js"),
        sourcemap: "external",
        sourcesContent: true,
        treeShaking: false,
      });
      const map = new TraceMap(JSON.parse(built.map));
      for (const statement of statements(built.code)) {
        const position = originalPositionFor(map, statement.start);
        if (position.line !== null) lines.add(position.line);
      }
    }
    for (const fragment of file.javascript || []) {
      for (const statement of statements(fragment.code)) {
        const line = fragment.lines[statement.start.line - 1];
        if (line !== undefined) lines.add(line);
      }
    }
    file.lines = [...lines].filter((line) => !file.excluded.includes(line)).sort((a, b) => a - b);
  }
}

// Convert generated line coverage first then map statements individually so ranges cannot bridge unrelated Python files
async function convert(directory) {
  const inventory = JSON.parse(fs.readFileSync(path.join(directory, "inventory.json")));
  const bundles = new Map();
  const results = [];
  const excluded = [];
  const errors = [];
  const attempts = fs.readdirSync(path.join(directory, "attempts")).sort();
  for (const attempt of attempts) {
    const attemptDir = path.join(directory, "attempts", attempt);
    const metadata = JSON.parse(fs.readFileSync(path.join(attemptDir, "attempt.json")));
    const bundleDir = path.join(attemptDir, "bundles");
    if (fs.existsSync(bundleDir))
      for (const name of fs.readdirSync(bundleDir).filter((name) => name.endsWith(".json"))) {
        const bundle = JSON.parse(fs.readFileSync(path.join(bundleDir, name)));
        bundles.set(bundle.url, bundle);
        excluded.push(...bundle.excluded);
        for (const [filename, snapshot] of Object.entries(bundle.sources)) {
          if (inventory.files[filename] && inventory.files[filename].sha256 !== snapshot.sha256) {
            throw new Error(`Source changed during coverage collection ${filename}`);
          }
          if (snapshot.origin && inventory.files[snapshot.origin[0]])
            inventory.files[filename] = {
              ...snapshot,
              display_path: `generated/${path.basename(filename)}`,
              language: "javascript",
              lines: [],
              excluded: [],
            };
        }
      }
    const rawDir = path.join(attemptDir, "raw");
    if (!fs.existsSync(rawDir)) continue;
    let mapped = false;
    for (const name of fs.readdirSync(rawDir).sort()) {
      const raw = JSON.parse(fs.readFileSync(path.join(rawDir, name)));
      const hits = {};
      for (const entry of raw.entries) {
        const extension = /\/(pret|metanno)\/static\//.exec(entry.url)?.[1];
        if (extension && !inventory.roots[extension]) continue;
        let map;
        if (entry.url.startsWith("pret-coverage:")) {
          const bundle = bundles.get(entry.url);
          if (!bundle) throw new Error(`Missing bundle snapshot ${entry.url}`);
          map = bundleMap(bundle, entry.source);
        } else {
          const inline =
            /^[ \t]*\/\/[#@] sourceMappingURL=data:application\/json[^,]*;base64,([A-Za-z0-9+/=]+)[ \t]*$/m.exec(
              entry.source
            );
          if (inline) map = JSON.parse(Buffer.from(inline[1], "base64").toString());
          if (!map && /\/(?:pret|metanno)\/static\/(?:lib_|client_)/.test(entry.url)) {
            throw new Error(`Missing coverage build source map for ${entry.url}`);
          }
        }
        const local = entry.url.startsWith("file:") ? fileURLToPath(entry.url) : entry.url;
        const filenames = map ? sourcePaths(map, entry, inventory) : [inventory.files[local] ? local : null];
        // Tests running only sources left out of the report, eg the Transcrypt runtime, still have mapped sources
        if (map?.sources.some((source) => inventory.skipped.some((file) => source.endsWith(path.basename(file)))))
          mapped = true;
        if (!filenames.some(Boolean)) continue;
        mapped = true;
        const trace = map ? new TraceMap(map) : null;
        // Removing map comments without moving offsets keeps the converter on generated JavaScript
        const code = entry.source.replace(/^[ \t]*\/\/[#@] sourceMappingURL=[^\n]*/gm, (match) =>
          " ".repeat(match.length)
        );
        const converter = v8ToIstanbul("/coverage-input.js", 0, { source: code });
        await converter.load();
        // V8 can omit functions that were untouched since the previous per test checkpoint
        converter.applyCoverage([
          {
            functionName: "",
            isBlockCoverage: true,
            ranges: [{ startOffset: 0, endOffset: code.length, count: 0 }],
          },
        ]);
        converter.applyCoverage(entry.functions);
        const coverage = Object.values(converter.toIstanbul())[0];
        const generatedHits = new Set(
          Object.entries(coverage.statementMap)
            .filter(([id]) => coverage.s[id] > 0)
            .map(([, statement]) => statement.start.line)
        );
        for (const statement of statements(code)) {
          if (!generatedHits.has(statement.start.line)) continue;
          const position = trace
            ? originalPositionFor(trace, statement.start)
            : { source: local, line: statement.start.line };
          const filename = trace ? filenames[trace.resolvedSources.indexOf(position.source)] : local;
          if (!filename || position.line === null) continue;
          (hits[filename] ||= new Set()).add(position.line);
        }
      }
      results.push({
        runtime: raw.runtime,
        test: raw.test,
        status: metadata.status,
        files: Object.fromEntries(Object.entries(hits).map(([filename, lines]) => [filename, [...lines]])),
      });
    }
    if (!mapped) errors.push(`No project sources mapped from frontend coverage for ${metadata.test}`);
  }
  inventoryLines(inventory);
  fs.writeFileSync(path.join(directory, "frontend.json"), JSON.stringify({ inventory, results, excluded, errors }));
}

if (require.main === module)
  convert(path.resolve(process.argv[2])).catch((error) => {
    console.error(error);
    process.exitCode = 1;
  });

module.exports = { inlineTranscrypt };
