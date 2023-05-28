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
                test: /\.m?js/,
                resolve: {
                    fullySpecified: false
                }
            },
        ],
    },
    resolve: {
        extensions: ['.tsx', '.ts', '.js', '.css'],
    },
    optimization: {
        usedExports: true,
    },
    cache: {
        type: 'filesystem',
    }
};
