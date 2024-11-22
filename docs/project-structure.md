# Kwt Project Structure

```plaintext
Kwt Project
├── .github/
│   └── workflows/
│       ├── build.yml
│       ├── eslint.yml
│       └── linters.yml
├── .vscode/
│   ├── settings.json
│   └── sftp.json
├── korfbal/
│   ├── __init__.py
│   ├── asgi.py
│   ├── settings.py
│   ├── urls.py
│   └── wsgi.py
├── apps/
│   ├── club/
│   │   ├── admin/
│   │   │   ├── __init__.py
│   │   │   └── club_admin.py
│   │   ├── consumers/
│   │   │   ├── __init__.py
│   │   │   └── club_data.py
│   │   ├── migrations/
│   │   │   ├── 0001_initial.py
│   │   │   ├── 0002_initial.py
│   │   │   ├── 0003_alter_club_logo.py
│   │   │   └── __init__.py
│   │   ├── models/
│   │   │   ├── __init__.py
│   │   │   ├── club.py
│   │   │   └── club_admin.py
│   │   ├── views/
│   │   │   ├── __init__.py
│   │   │   └── club_detail.py
│   │   ├── __init__.py
│   │   ├── apps.py
│   │   ├── routing.py
│   │   └── urls.py
│   ├── common/
│   │   ├── context_processors/
│   │   │   ├── __init__.py
│   │   │   └── standart_imports.py
│   │   ├── management/
│   │   │   └── commands/
│   │   │       └── webpack_collectstatic.py
│   │   ├── middleware/
│   │   │   ├── __init__.py
│   │   │   └── visitor_tracking.py
│   │   ├── templatetags/
│   │   │   ├── __init__.py
│   │   │   ├── replace.py
│   │   │   └── truncate_middle.py
│   │   ├── utils/
│   │   │   ├── __init__.py
│   │   │   ├── general_stats.py
│   │   │   ├── players_stats.py
│   │   │   └── transform_matchdata.py
│   │   ├── __init__.py
│   │   └── apps.py
│   ├── game_tracker/
│   │   ├── admin/
│   │   │   ├── __init__.py
│   │   │   ├── goal_type_admin.py
│   │   │   ├── group_type_admin.py
│   │   │   ├── match_data_admin.py
│   │   │   ├── match_part_admin.py
│   │   │   ├── match_player_admin.py
│   │   │   ├── pause_admin.py
│   │   │   ├── player_change_admin.py
│   │   │   ├── player_group_admin.py
│   │   │   └── shot_admin.py
│   │   ├── consumers/
│   │   │   ├── __init__.py
│   │   │   ├── common.py
│   │   │   ├── match_data.py
│   │   │   └── match_tracker.py
│   │   ├── froms/
│   │   │   ├── __init__.py
│   │   │   └── player_group_form.py
│   │   ├── migrations/
│   │   │   ├── 0001_initial.py
│   │   │   ├── 0002_initial.py
│   │   │   ├── 0003_matchpart_active.py
│   │   │   ├── 0004_pause_active.py
│   │   │   ├── 0005_remove_matchdata_players_matchplayer.py
│   │   │   ├── 0006_grouptypes_order.py
│   │   │   └── __init__.py
│   │   ├── models/
│   │   │   ├── __init__.py
│   │   │   ├── constants.py
│   │   │   ├── goal_type.py
│   │   │   ├── group_type.py
│   │   │   ├── match_data.py
│   │   │   ├── match_part.py
│   │   │   ├── match_player.py
│   │   │   ├── pause.py
│   │   │   ├── player_change.py
│   │   │   ├── player_group.py
│   │   │   └── shot.py
│   │   ├── signals/
│   │   │   ├── __init__.py
│   │   │   ├── match_signals.py
│   │   │   └── matchdata_signals.py
│   │   ├── views/
│   │   │   ├── __init__.py
│   │   │   ├── common.py
│   │   │   ├── match_detail.py
│   │   │   ├── match_team_selector.py
│   │   │   ├── match_tracker.py
│   │   │   └── player_selection.py
│   │   ├── __init__.py
│   │   ├── apps.py
│   │   ├── routing.py
│   │   └── urls.py
│   ├── hub/
│   │   ├── admin/
│   │   │   ├── __init__.py
│   │   │   └── page_connect_registration_admin.py
│   │   ├── migrations/
│   │   │   ├── 0001_initial.py
│   │   │   ├── 0002_initial.py
│   │   │   └── __init__.py
│   │   ├── models/
│   │   │   ├── __init__.py
│   │   │   ├── constants.py
│   │   │   └── page_connect_registration.py
│   │   ├── views/
│   │   │   ├── __init__.py
│   │   │   ├── catalog.py
│   │   │   ├── catalog_data.py
│   │   │   ├── index.py
│   │   │   ├── previous_page.py
│   │   │   └── search.py
│   │   ├── __init__.py
│   │   ├── apps.py
│   │   └── urls.py
│   ├── player/
│   │   ├── admin/
│   │   │   ├── __init__.py
│   │   │   └── player_admin.py
│   │   ├── consumers/
│   │   │   ├── __init__.py
│   │   │   └── profile_data.py
│   │   ├── migrations/
│   │   │   ├── 0001_initial.py
│   │   │   ├── 0002_initial.py
│   │   │   ├── 0003_alter_player_profile_picture.py
│   │   │   └── __init__.py
│   │   ├── models/
│   │   │   ├── __init__.py
│   │   │   ├── constants.py
│   │   │   └── player.py
│   │   ├── signals/
│   │   │   ├── __init__.py
│   │   │   └── player_signals.py
│   │   ├── views/
│   │   │   ├── __init__.py
│   │   │   ├── profile_detail.py
│   │   │   └── upload_profile_picture.py
│   │   ├── __init__.py
│   │   ├── apps.py
│   │   ├── routing.py
│   │   └── urls.py
│   ├── schedule/
│   │   ├── admin/
│   │   │   ├── __init__.py
│   │   │   ├── match_admin.py
│   │   │   └── season_admin.py
│   │   ├── migrations/
│   │   │   ├── 0001_initial.py
│   │   │   ├── 0002_initial.py
│   │   │   └── __init__.py
│   │   ├── models/
│   │   │   ├── __init__.py
│   │   │   ├── constants.py
│   │   │   ├── match.py
│   │   │   └── season.py
│   │   ├── __init__.py
│   │   └── apps.py
│   └── team/
│       ├── admin/
│       │   ├── __init__.py
│       │   ├── team_admin.py
│       │   └── team_data_admin.py
│       ├── consumers/
│       │   ├── __init__.py
│       │   └── team_data.py
│       ├── migrations/
│       │   ├── 0001_initial.py
│       │   └── __init__.py
│       ├── models/
│       │   ├── __init__.py
│       │   ├── constants.py
│       │   ├── team.py
│       │   └── team_data.py
│       ├── views/
│       │   ├── __init__.py
│       │   ├── register_to_team.py
│       │   └── team_detail.py
│       ├── __init__.py
│       ├── apps.py
│       ├── routing.py
│       └── urls.py
├── docs/
│   └── project-structure.md
├── static_workfile/
│   ├── css/
│   │   ├── authentication/
│   │   │   ├── 2fa_code.css
│   │   │   └── login_registration.css
│   │   ├── clubs/
│   │   │   └── detail.css
│   │   ├── color/
│   │   │   └── color.css
│   │   ├── matches/
│   │   │   ├── detail.css
│   │   │   ├── players_selector.css
│   │   │   └── tracker.css
│   │   ├── overlays/
│   │   │   └── navbar.css
│   │   ├── teams/
│   │   │   └── index.css
│   │   └── index.css
│   ├── images/
│   │   ├── clubs/
│   │   │   └── blank-club-picture.png
│   │   ├── logo/
│   │   │   ├── ButrosGrootLogo2.svg
│   │   │   └── KWT_logo.png
│   │   ├── navbar/
│   │   │   ├── group.svg
│   │   │   ├── home.svg
│   │   │   ├── login.svg
│   │   │   └── profile.svg
│   │   ├── player/
│   │   │   ├── blank-profile-picture.png
│   │   │   └── edit-button-svgrepo-com.svg
│   │   ├── arrow-left.svg
│   │   ├── arrow.svg
│   │   ├── heart-full.svg
│   │   ├── heart-outline.svg
│   │   ├── search.svg
│   │   └── settings.svg
│   ├── js/
│   │   ├── clubs/
│   │   │   └── club_detail.js
│   │   ├── common/
│   │   │   ├── carousel/
│   │   │   │   ├── events_utils/
│   │   │   │   │   ├── events.js
│   │   │   │   │   ├── index.js
│   │   │   │   │   ├── on_player_select_change.js
│   │   │   │   │   ├── player_groups.js
│   │   │   │   │   └── save_player_groups.js
│   │   │   │   ├── utils/
│   │   │   │   │   ├── clean_dom_carousel.js
│   │   │   │   │   ├── handle_button_click.js
│   │   │   │   │   ├── handle_touch_end.js
│   │   │   │   │   ├── handle_touch_move.js
│   │   │   │   │   ├── handle_touch_start.js
│   │   │   │   │   └── index.js
│   │   │   │   ├── index.js
│   │   │   │   ├── setup_carousel.js
│   │   │   │   ├── show_player_groups.js
│   │   │   │   ├── update_events.js
│   │   │   │   ├── update_goal_stats.js
│   │   │   │   ├── update_matches.js
│   │   │   │   ├── update_player_groups.js
│   │   │   │   ├── update_players.js
│   │   │   │   ├── update_settings.js
│   │   │   │   ├── update_statastics.js
│   │   │   │   └── update_team.js
│   │   │   ├── profile_picture/
│   │   │   │   ├── utils/
│   │   │   │   │   ├── index.js
│   │   │   │   │   └── upload_image.js
│   │   │   │   ├── index.js
│   │   │   │   └── setup_profile_picture.js
│   │   │   ├── swipeDelete/
│   │   │   │   ├── utils/
│   │   │   │   │   ├── delete_confirm_popup.js
│   │   │   │   │   └── index.js
│   │   │   │   ├── delete_button_setup.js
│   │   │   │   ├── index.js
│   │   │   │   ├── reset_swipe.js
│   │   │   │   └── setup_swipe_delete.js
│   │   │   ├── utils/
│   │   │   │   ├── index.js
│   │   │   │   ├── truncate_middle.js
│   │   │   │   └── wedstrijd_punten.js
│   │   │   ├── websockets/
│   │   │   │   ├── index.js
│   │   │   │   ├── initialize_socket.js
│   │   │   │   └── request_inital_data.js
│   │   │   ├── common.js
│   │   │   └── setup_follow_button.js
│   │   ├── hub/
│   │   │   └── hub_catalog.js
│   │   ├── matches/
│   │   │   ├── common/
│   │   │   │   ├── countdown_timer.js
│   │   │   │   └── index.js
│   │   │   ├── match_detail.js
│   │   │   ├── match_tracker.js
│   │   │   └── players_selector.js
│   │   ├── profile/
│   │   │   └── profile_detail.js
│   │   ├── programs/
│   │   │   └── mobile_view.js
│   │   ├── pwa/
│   │   │   └── service-worker.js
│   │   └── teams/
│   │       └── team_detail.js
│   └── json/
│       └── manifest.json
├── templates/
│   ├── authentication/
│   │   ├── email_template/
│   │   │   ├── 2fa_email.html
│   │   │   └── activation_email.html
│   │   ├── registration/
│   │   │   ├── password_reset_complete.html
│   │   │   ├── password_reset_confirm.html
│   │   │   ├── password_reset_done.html
│   │   │   └── password_reset_form.html
│   │   ├── confirmation_sent.html
│   │   ├── enter_2fa_code.html
│   │   ├── login.html
│   │   └── register.html
│   ├── club/
│   │   └── detail.html
│   ├── common/
│   │   └── imports.html
│   ├── hub/
│   │   ├── catalog.html
│   │   └── index.html
│   ├── matches/
│   │   ├── detail.html
│   │   ├── players_selector.html
│   │   ├── team_selector.html
│   │   └── tracker.html
│   ├── overlays/
│   │   ├── footer.html
│   │   └── navbar.html
│   ├── profile/
│   │   └── index.html
│   └── teams/
│       └── detail.html
├── .flake8
├── .gitignore
├── .pre-commit-config.yaml
├── Dockerfile-daphne
├── Dockerfile-uwsgi
├── eslint.config.mjs
├── generate_project_structure.py
├── kwt-daphne.service
├── kwt-uwsgi.service
├── manage.py
├── package-lock.json
├── package.json
├── pyproject.toml
├── readme.md
├── reload.flag
├── requirements.txt
├── sonar-project.properties
├── uwsgi.ini
├── webpack-stats.json
└── webpack.config.js
```
