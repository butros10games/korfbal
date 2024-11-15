const path = require('path');
const BundleTracker = require('webpack-bundle-tracker');

module.exports = {
  entry: './static_workfile/js/index.js',
  output: {
    path: path.resolve(__dirname, 'static_workfile/webpack_bundles'), // Output directory
    filename: '[name]-[contenthash].js', // Use [contenthash] instead of [hash]
    publicPath: '/static_workfile/webpack_bundles/', // Public URL for assets
  },
  module: {
    rules: [
      {
        test: /\.js$/,
        exclude: /node_modules/,
        use: {
          loader: 'babel-loader',
          options: {
            presets: ['@babel/preset-env'],
          },
        },
      },
    ],
  },
  plugins: [
    new BundleTracker({
      path: __dirname, // Directory where webpack-stats.json will be saved
      filename: 'webpack-stats.json', // File name without any path
    }),
  ],
  mode: 'development', // Change to 'production' for optimized builds
};
