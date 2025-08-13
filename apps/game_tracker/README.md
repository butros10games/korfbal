<!-- Badges: Uncomment and update as needed -->
<!--
![Build Status](https://img.shields.io/github/workflow/status/butros10games/MonoRepo/CI)
![Coverage](https://img.shields.io/codecov/c/github/butros10games/MonoRepo)
![License](https://img.shields.io/github/license/butros10games/MonoRepo)
-->

# game_tracker (Django app)

## Features

- Game tracking and statistics for the korfbal project.

## Requirements

- Django >= 3.2

## Usage

Add to `INSTALLED_APPS` in your Django settings:

```python
INSTALLED_APPS = [
    ...
    'game_tracker',
]
```

## Local test

Run from the project root:

- uv run python manage.py test game_tracker

## Contributing

Contributions are welcome! Please see the main [Contributing Guide](../../../../../../docs/contributing.md) for workflow and code style.

<!-- Optionally add a screenshot or architecture diagram here -->
