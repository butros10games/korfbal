# Kwt Project Structure

```plaintext
Kwt Project
├── apps/
│   ├── club/
│   │   ├── __init__.py
│   │   ├── admin.py
│   │   ├── apps.py
│   │   ├── consumers.py
│   │   ├── models.py
│   │   ├── routing.py
│   │   ├── tests.py
│   │   ├── urls.py
│   │   └── views.py
│   ├── game_tracker/
│   │   ├── __init__.py
│   │   ├── admin.py
│   │   ├── apps.py
│   │   ├── consumers.py
│   │   ├── form.py
│   │   ├── models.py
│   │   ├── routing.py
│   │   ├── signals.py
│   │   ├── tests.py
│   │   ├── urls.py
│   │   └── views.py
│   ├── hub/
│   │   ├── middleware/
│   │   │   ├── __init__.py
│   │   │   └── visitor_tracking.py
│   │   ├── templatetags/
│   │   │   ├── __init__.py
│   │   │   └── custom_tags.py
│   │   ├── views/
│   │   │   ├── __init__.py
│   │   │   ├── catalog_data.py
│   │   │   ├── catalog.py
│   │   │   ├── index.py
│   │   │   ├── previous_page.py
│   │   │   └── search.py
│   │   ├── __init__.py
│   │   ├── admin.py
│   │   ├── apps.py
│   │   ├── context_processors.py
│   │   ├── models.py
│   │   ├── tests.py
│   │   └── urls.py
│   ├── player/
│   │   ├── __init__.py
│   │   ├── admin.py
│   │   ├── apps.py
│   │   ├── consumers.py
│   │   ├── models.py
│   │   ├── routing.py
│   │   ├── signals.py
│   │   ├── tests.py
│   │   ├── urls.py
│   │   └── views.py
│   ├── schedule/
│   │   ├── __init__.py
│   │   ├── admin.py
│   │   ├── apps.py
│   │   ├── models.py
│   │   ├── tests.py
│   │   ├── urls.py
│   │   └── views.py
│   └── team/
│   │   ├── __init__.py
│   │   ├── admin.py
│   │   ├── apps.py
│   │   ├── consumers.py
│   │   ├── models.py
│   │   ├── routing.py
│   │   ├── tests.py
│   │   ├── urls.py
│   │   └── views.py
├── Korfbal/
│   ├── __init__.py
│   ├── asgi.py
│   ├── settings.py
│   ├── urls.py
│   ├── private_settings.json
│   └── wsgi.py
├── static_workfile/
│   └── json/
│       └── manifest.json
├── templates/
│   ├── club/
│   │   └── detail.html
│   ├── overlays/
│   │   ├── navbar.html
│   │   └── footer.html
│   ├── matches/
│   │   ├── team_selector.html
│   │   ├── tracker.html
│   │   └── detail.html
│   ├── profile/
│   │   └── index.html
│   ├── teams/
│   │   └── detail.html
│   ├── authentication/
│   │   ├── confirmation_sent.html
│   │   ├── register.html
│   │   ├── login.html
│   │   ├── enter_2fa_code.html
│   │   ├── registration/
│   │   │   ├── password_reset_done.html
│   │   │   ├── password_reset_confirm.html
│   │   │   ├── password_reset_form.html
│   │   │   └── password_reset_complete.html
│   │   └── email_template/
│   │       ├── activation_email.html
│   │       └── 2fa_email.html
│   └── hub/
│       ├── index.html
│       └── catalog.html
├── docs/
│   └── project-structure.md
├── .gitignore
├── Dockerfile-daphne
├── Dockerfile-uwsgi
├── kwt-daphne.service
├── kwt-uwsgi.service
├── manage.py
├── requirements.txt
├── sonar-project.properties
└── uwsgi.ini
```