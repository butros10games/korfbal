"""Custom Django management command to run Webpack and collect static files."""

from subprocess import call

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    """Custom Django management command to run Webpack and collect static files."""

    help = "Run Webpack and collect static files"

    def handle(self, *args, **kwargs):
        """Handle the command."""
        target_directory = "static_workfile/"
        start_directory = "static_build/"

        self.stdout.write("Copying files from build directory to static directory...")
        call(["cp", "-r", f"{start_directory}css/", target_directory])
        call(["cp", "-r", f"{start_directory}images/", target_directory])
        call(["cp", "-r", f"{start_directory}json/", target_directory])
        self.stdout.write("Files copied.")

        self.stdout.write("Running Webpack...")
        call(["npx", "webpack", "--config", "webpack.config.js"])
        self.stdout.write("Webpack build completed.")

        self.stdout.write("Collecting static files...")
        call(["python", "manage.py", "collectstatic", "--noinput"])
        self.stdout.write("Static files collected.")
