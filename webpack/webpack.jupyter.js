const webpack = require("webpack");
const path = require("path");

module.exports = {
  module: {
    rules: [
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
    usedExports: true
  },
  cache: {
    type: "filesystem"
  }
};
