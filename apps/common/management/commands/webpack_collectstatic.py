from subprocess import call

from django.core.management.base import BaseCommand


class Command(BaseCommand):
    help = "Run Webpack and collect static files"

    def handle(self, *args, **kwargs):
        self.stdout.write("Running Webpack...")
        call(["npx", "webpack", "--config", "webpack.config.js"])
        self.stdout.write("Webpack build completed.")

        self.stdout.write("Collecting static files...")
        call(["python", "manage.py", "collectstatic", "--noinput"])
        self.stdout.write("Static files collected.")
