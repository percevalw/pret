const path = require("path");
const webpack = require("webpack");
const { ModuleFederationPlugin } = webpack.container;
const HtmlWebpackPlugin = require("html-webpack-plugin");

module.exports = (env) => ({
  entry: path.resolve("./client/standalone/index.tsx"),
  devtool: process.env.PRET_COVERAGE ? "inline-source-map" : "eval-source-map",
  module: {
    rules: [
      { test: /\.wasm$/, type: "asset/resource" },
      {
        test: /\.[jt]sx?$/,
        use: "ts-loader",
        exclude: /node_modules/
      },
      {
        test: /\.(css)$/,
        use: ["style-loader", "css-loader"]
      },
      {
        test: /\.py$/,
        type: "asset/inline",
        generator: {
          dataUrl: content => content.toString()
        }
      },
      {
        test: /\.m?js/,
        resolve: {
          fullySpecified: false
        }
      },
    ]
  },
  resolve: {
    extensions: [".tsx", ".ts", ".js", ".py", ".css"],
    alias: {
      "@pret-globals": path.resolve("./client/globals.ts")
    },
  },
  output: {
    filename: "bundle.[contenthash].js",
    // reuse the static dir from the jupyterlab extension
    // we could hardcode another path here but this will be more consistent
    // with extensions
    path: path.resolve("pret/js-base")
  },
  plugins: [
    new webpack.IgnorePlugin({
      resourceRegExp: /^(node:|node-fetch)/, // Ignore all imports that start with "node:"
    }),
    new HtmlWebpackPlugin({
      title: "Pret",
      template: "./client/standalone/index.ejs",
      inject: false,
      // pret/main.py fills the placeholders of this page by text replacement, keep its layout
      minify: false,
      publicPath: '/assets',
      templateParameters: (compilation, assets, assetTags, options) => {
        return {
          compilation: compilation,
          webpack: compilation.getStats().toJson(),
          webpackConfig: compilation.options,
          htmlWebpackPlugin: {
            tags: assetTags,
            files: assets,
            options: options
          },
          __PRET_BUNDLE_BYTES_FILE__: process.env.PRET_BUNDLE_BYTES_FILE || "__PRET_BUNDLE_BYTES_FILE__",
          __PRET_BUNDLE_CODE_FILE__: process.env.PRET_BUNDLE_CODE_FILE || "__PRET_BUNDLE_CODE_FILE__"
        };
      }
    }),
    new webpack.ProvidePlugin({
      process: "process"
    }),
    new ModuleFederationPlugin({
      name: "PRET",
      shared: {
        react: {
          eager: true, requiredVersion: "^17.0.1", singleton: true
        },
        "react-dom": {
          eager: true, requiredVersion: "^17.0.1", singleton: true
        },
        // Extension bundles import pret, which JupyterLab provides through the pret extension
        pret: {
          eager: true,
          singleton: true,
          import: path.resolve("./client/standalone/shared.ts"),
          version: require("../package.json").version,
          requiredVersion: false,
        }
      }
    })
  ],
  optimization: {
    usedExports: !process.env.PRET_COVERAGE,
    minimize: process.env.PRET_COVERAGE ? false : undefined,
  },
  cache: {
    type: "filesystem"
  },
});
