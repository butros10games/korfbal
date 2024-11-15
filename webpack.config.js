const path = require('path');
const BundleTracker = require('webpack-bundle-tracker');

module.exports = {
  entry: {
    hub_index: './static_workfile/js/index.js',
    club_detail: './static_workfile/js/clubs/club_detail.js',
    match_detail: './static_workfile/js/matches/match_detail.js',
    players_selector: './static_workfile/js/matches/player_selector.js',
    match_tracker: './static_workfile/js/matches/match_tracker.js',
    profile_detail: './static_workfile/js/profiles/profile_detail.js',
    teams_detail: './static_workfile/js/teams/team_detail.js',
  },
  output: {
    path: path.resolve(__dirname, 'static_workfile/webpack_bundles'), // Output directory
    filename: '[name].bundle.js', // Use [contenthash] instead of [hash]
    publicPath: '/static_workfile/webpack_bundles/', // Public URL for assets
    clean: true,
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
  mode: 'production',
};
