const path = require('path');

module.exports = {
    entry: {
        hub_catalog: './static_workfile/js/features/hub/hubCatalog.js',
        landing: './static_workfile/js/features/hub/landing.js',
        club_detail: './static_workfile/js/features/clubs/clubDetail.js',
        match_detail: './static_workfile/js/features/matches/matchDetail.js',
        players_selector: './static_workfile/js/features/matches/playersSelector.js',
        match_tracker: './static_workfile/js/features/matches/matchTracker.js',
        profile_detail: './static_workfile/js/features/profile/profileDetail.js',
        teams_detail: './static_workfile/js/features/teams/teamDetail.js',
        mobile_view: './static_workfile/js/features/mobile/mobileView.js',
        service_worker: './static_workfile/js/features/pwa/serviceWorker.js',
        navbar: './static_workfile/js/features/navbar/navbar.js',
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
