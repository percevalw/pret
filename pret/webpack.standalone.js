const path = require('path');
const HtmlWebpackPlugin = require("html-webpack-plugin");

module.exports = (env) => ({
    entry: env.pretGlobalsFile,
    devtool: 'eval-source-map',
    module: {
        rules: [
            {
                test: /\.[jt]sx?$/,
                use: 'ts-loader',
                exclude: /node_modules/,
            },
            {
                test: /\.(css)$/,
                use: ['style-loader', 'css-loader'],
            },
            {
                test: /\.py$/,
                type: 'asset/inline',
                generator: {
                    dataUrl: content => content.toString(),
                },
            },
            {
                test: /\.m?js/,
                resolve: {
                    fullySpecified: false
                }
            },
        ],
    },
    resolve: {
        extensions: ['.tsx', '.ts', '.js', '.py', '.css'],
        alias: {
            '@pret-globals': env.pretGlobalsFile,
        }
    },
    output: {
        filename: 'bundle.js',
        path: path.resolve(__dirname, 'static'),
    },
    plugins: [
        new HtmlWebpackPlugin({
            title: 'Pret',
            template: "./client/standalone/index.ejs",
            inject: false,
            templateParameters: (compilation, assets, assetTags, options) => {
                return {
                    compilation: compilation,
                    webpack: compilation.getStats().toJson(),
                    webpackConfig: compilation.options,
                    htmlWebpackPlugin: {
                        tags: assetTags,
                        files: assets,
                        options: options,
                    },
                    __PRET_PICKLE_FILE__: process.env.PRET_PICKLE_FILE || '__PRET_PICKLE_FILE__',
                };
            },
        }),
    ],
    optimization: {
        usedExports: true,
    },
    cache: {
        type: 'filesystem',
    }
});
