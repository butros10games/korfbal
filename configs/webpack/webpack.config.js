const path = require('path');

module.exports = {
    entry: {
        hub_catalog: './static_build/js/features/hub/hubCatalog.js',
        club_detail: './static_build/js/features/clubs/clubDetail.js',
        match_detail: './static_build/js/features/matches/matchDetail.js',
        players_selector: './static_build/js/features/matches/playersSelector.js',
        match_tracker: './static_build/js/features/matches/matchTracker.js',
        profile_detail: './static_build/js/features/profile/profileDetail.js',
        teams_detail: './static_build/js/features/teams/teamDetail.js',
        mobile_view: './static_build/js/features/mobile/mobileView.js',
        service_worker: './static_build/js/features/pwa/serviceWorker.js',
    },
    output: {
        path: path.resolve(__dirname, 'static_workfile/webpack_bundles'),
        filename: '[name].bundle.js',
        publicPath: '../../static_workfile/webpack_bundles/',
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
    resolve: {
        alias: {
            Components: path.resolve(__dirname, 'js/components/'),
            Features: path.resolve(__dirname, 'js/features/'),
            Utils: path.resolve(__dirname, 'js/utils/'),
        },
    },
    mode: 'production',
};
