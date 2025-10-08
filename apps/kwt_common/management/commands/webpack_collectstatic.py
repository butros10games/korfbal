"""Custom Django management command to run Webpack and collect static files."""

import os
import shutil
import subprocess  # nosec B404: Required for invoking local webpack via npx (controlled arguments)

from django.core.management import call_command
from django.core.management.base import BaseCommand


class Command(BaseCommand):
    """Custom Django management command to run Webpack and collect static files."""

    help = "Run Webpack and collect static files"

    def handle(self, *args: tuple[object, ...], **kwargs: dict[str, object]) -> None:
        """Handle the command.

        Raises:
            RuntimeError: If npx or webpack.config.js is not found, or if the Web
            pack build fails.

        """
        target_directory = "static_workfile/"
        start_directory = "static_build/"

        self.stdout.write("Copying files from build directory to static directory...")
        os.makedirs(target_directory, exist_ok=True)
        for sub in ("css", "images", "json"):
            src = os.path.join(start_directory, sub)
            dst = os.path.join(target_directory, sub)
            if not os.path.exists(src):
                self.stdout.write(f"Source not found, skipping: {src}")
                continue
            try:
                shutil.copytree(src, dst, dirs_exist_ok=True)
            except TypeError:
                if os.path.exists(dst):
                    shutil.rmtree(dst)
                shutil.copytree(src, dst)
        self.stdout.write("Files copied.")

        self.stdout.write("Running Webpack...")
        npx = shutil.which("npx")
        if not npx:
            raise RuntimeError("npx executable not found in PATH; cannot run webpack")

        config_path = os.path.abspath("webpack.config.js")
        project_root = os.path.abspath(os.getcwd())
        if not config_path.startswith(project_root):  # basic containment check
            raise RuntimeError("Webpack config path escapes project root; aborting")
        if not os.path.isfile(config_path):
            raise RuntimeError("webpack.config.js not found; cannot run webpack")

        cmd = [npx, "webpack", "--config", config_path]
        try:
            subprocess.run(cmd, check=True)  # nosec B603: command list is fully static & validated
        except subprocess.CalledProcessError as exc:
            raise RuntimeError(f"Webpack build failed: {exc}") from exc
        self.stdout.write("Webpack build completed.")

        self.stdout.write("Collecting static files...")
        call_command("collectstatic", interactive=False)
        self.stdout.write("Static files collected.")
