"""Re-run a finished replay's match pass and publish its match-wide names."""

from argparse import ArgumentParser
import json

from django.core.management.base import BaseCommand

from apps.video_analysis import composition
from apps.video_analysis.services import match_identity


class Command(BaseCommand):
    """Run the match pass through the production path, synchronously.

    The inputs are restored from object storage, the pass runs in the vision
    runtime under the worker's storage lease, and the replay's renamed links
    are published, exactly as the task a reviewer's answer queues.
    """

    help = __doc__

    def add_arguments(self, parser: ArgumentParser) -> None:
        """Accept the replay's clip job ID."""
        parser.add_argument("replay", help="The replay's clip job ID")

    def handle(self, *args: object, **options: object) -> None:
        """Stage the inputs, solve in the vision runtime and publish the names."""
        del args
        receipt = match_identity.republish(
            str(options["replay"]), composition.match_identity_runtime(), force=True
        )
        self.stdout.write(json.dumps(receipt))
