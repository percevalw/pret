const path = require('path');

module.exports = {
    module: {
        rules: [
            {
                test: /\.[jt]sx?$/,
                use: 'ts-loader',
                exclude: /node_modules/,
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
    },
    optimization: {
        usedExports: true,
    },
    cache: {
        type: 'filesystem',
    }
};
