<!-- Badges: Uncomment and update as needed -->
<!--
![Build Status](https://img.shields.io/github/workflow/status/butros10games/MonoRepo/CI)
![Coverage](https://img.shields.io/codecov/c/github/butros10games/MonoRepo)
![License](https://img.shields.io/github/license/butros10games/MonoRepo)
-->

# hub

## Features
- Central hub for user and team management in the korfbal project.

## Requirements
- Django >= 3.2

## Usage
Add to `INSTALLED_APPS` in your Django settings:
```python
INSTALLED_APPS = [
    ...
    'hub',
]
```

## Testing
To run tests for this app:
```bash
python manage.py test hub
```
Make sure you have a test database configured.

## Contributing
Contributions are welcome! Please see the main [Contributing Guide](../../../../../../docs/contributing.md) for workflow and code style.

<!-- Optionally add a screenshot or architecture diagram here -->
