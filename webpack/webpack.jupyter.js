const webpack = require("webpack");
const path = require("path");

// The JupyterLab builder collects licenses with license-webpack-plugin 2 in production builds, which reads
// "provide module" identifiers as "... = path" and fails on the identifiers of current webpack, written as
// "provide module (default) name@version|path", the path after "|" is returned instead
const { WebpackModuleFileIterator } = require(require.resolve("license-webpack-plugin/dist/WebpackModuleFileIterator", {
  paths: [path.dirname(require.resolve("@jupyterlab/builder/package.json"))],
}));
const getActualFilename = WebpackModuleFileIterator.prototype.getActualFilename;
WebpackModuleFileIterator.prototype.getActualFilename = function (filename) {
  if (filename && filename.indexOf("provide module") === 0 && filename.indexOf("=") < 0) {
    return filename.slice(filename.lastIndexOf("|") + 1);
  }
  return getActualFilename.call(this, filename);
};

module.exports = {
  ...(process.env.PRET_COVERAGE ? { devtool: "inline-source-map" } : {}),
  module: {
    rules: [
      // Carry the library build maps back to TypeScript in coverage assets
      ...(process.env.PRET_COVERAGE ? [{
        test: /\.js$/, enforce: "pre", use: "source-map-loader", exclude: /node_modules/,
      }] : []),
      { test: /\.wasm$/, type: "asset/resource" },
      {
        test: /\.[jt]sx?$/,
        use: "ts-loader",
        exclude: /node_modules/
      },
      {
        test: /\.py$/,
        type: "asset/inline",
        generator: {
          dataUrl: content => content.toString()
        }
      },
      // Adapted from:
      // https://github.com/pyodide/pyodide-webpack-plugin/blob/9112d85/README.md?plain=1#L100-L119
      // to replace all import occurrences instead of just the first one
      {
        test: /\.m?js/,
        resolve: {
          fullySpecified: false
        }
      },
    ]
  },
  resolve: {
    extensions: [".tsx", ".ts", ".js", ".py", ".css", ".mjs"],
    alias: { "@pret-globals": path.resolve("client/globals.ts") }
  },
  plugins: [
    new webpack.IgnorePlugin({
      resourceRegExp: /^(node:|node-fetch)/, // Ignore all imports that start with "node:"
    }),
    new webpack.ProvidePlugin({
      process: "process"
    })
  ],
  optimization: {
    usedExports: !process.env.PRET_COVERAGE,
    minimize: process.env.PRET_COVERAGE ? false : undefined,
  },
  cache: {
    type: "filesystem"
  }
};
