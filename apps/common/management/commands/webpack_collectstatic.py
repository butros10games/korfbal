"""Custom Django management command to run Webpack and collect static files."""

from subprocess import call

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    """Custom Django management command to run Webpack and collect static files."""

    help = "Run Webpack and collect static files"

    def handle(self, *args, **kwargs):
        """Handle the command."""
        self.stdout.write("Running Webpack...")
        call(["npx", "webpack", "--config", "webpack.config.js"])
        self.stdout.write("Webpack build completed.")

        self.stdout.write("Collecting static files...")
        call(["python", "manage.py", "collectstatic", "--noinput"])
        self.stdout.write("Static files collected.")
