import path from 'path';

export default {
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
        path: path.resolve('static_workfile/webpack_bundles'),
        filename: '[name].bundle.js',
        publicPath: '../../static_workfile/webpack_bundles/',
        clean: true,
    },
    module: {
        rules: [
            {
                test: /\.js$/,
                exclude: /node_modules/,
            },
        ],
    },
    resolve: {
        alias: {
            Components: path.resolve('js/components/'),
            Features: path.resolve('js/features/'),
            Utils: path.resolve('js/utils/'),
        },
    },
    mode: 'production',
};