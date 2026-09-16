"""Local synthetic proxy configuration and explicit Linux CPU partitions."""

import argparse
import os


def cpu_set(value: str) -> str:
    """Validate a comma-separated set of CPUs available to this process.

    Raises:
        argparse.ArgumentTypeError: CPUs are invalid or unavailable.

    """
    try:
        cpus = {int(item) for item in value.split(",")}
    except ValueError as error:
        raise argparse.ArgumentTypeError("Use comma-separated CPU numbers.") from error
    if not hasattr(os, "sched_getaffinity") or not cpus <= os.sched_getaffinity(0):
        raise argparse.ArgumentTypeError("CPU partition is unavailable on this host.")
    return ",".join(map(str, sorted(cpus)))


def caddy_config(origin: str, api: str, live: str, sse: str, public: str) -> str:
    """Mirror documented production API routing, bound to loopback without TLS."""
    reads = "^/api/matches/[0-9a-fA-F-]+/(summary|stats|events|shots)/$"
    return f"""{{
    admin off
    auto_https off
}}
{origin} {{
    bind 127.0.0.1
    @match_sse path /api/live/events/
    @match_live {{
        method GET HEAD
        path_regexp match_live ^/api/matches/[0-9a-fA-F-]+/live(/poll)?/$
    }}
    @match_reads {{
        method GET HEAD
        path_regexp match_reads {reads}
    }}
    route {{
        reverse_proxy @match_sse {sse} {{
            flush_interval -1
        }}
        reverse_proxy @match_live {live}
        reverse_proxy @match_reads {public}
        reverse_proxy /api/* {api}
    }}
}}
"""


def add_proxy_arguments(parser: argparse.ArgumentParser) -> None:
    """Register optional owned-proxy and CPU-partition controls."""
    parser.add_argument(
        "--proxy",
        action="store_true",
        help="Route through owned local Caddy (HTTP/1.1, no TLS).",
    )
    parser.add_argument("--server-cpus", type=cpu_set)
    parser.add_argument("--generator-cpus", type=cpu_set)


def validate_cpu_partitions(
    parser: argparse.ArgumentParser, options: argparse.Namespace
) -> None:
    """Require disjoint explicit server and generator partitions."""
    if bool(options.server_cpus) != bool(options.generator_cpus):
        parser.error("Provide both server and generator CPU partitions.")
    if options.server_cpus and set(options.server_cpus.split(",")) & set(
        options.generator_cpus.split(",")
    ):
        parser.error("Server and generator CPU partitions must not overlap.")
