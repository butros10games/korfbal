const path = require('path');

module.exports = {
  entry: {
    hub_catalog: './static_workfile/js/hub/hub_catalog.js',
    club_detail: './static_workfile/js/clubs/club_detail.js',
    match_detail: './static_workfile/js/matches/match_detail.js',
    players_selector: './static_workfile/js/matches/players_selector.js',
    match_tracker: './static_workfile/js/matches/match_tracker.js',
    profile_detail: './static_workfile/js/profile/profile_detail.js',
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
  mode: 'production',
};
