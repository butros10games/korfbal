"""Restrict browser push destinations to supported provider origins."""

from urllib.parse import urlsplit


MAX_ENDPOINT_LENGTH = 1024


def validate_web_push_endpoint(endpoint: str) -> None:
    """Reject arbitrary destinations, including private addresses.

    Raises:
        ValueError: The URL is not a supported push provider endpoint.

    """
    try:
        url = urlsplit(endpoint)
        host = url.hostname or ""
        allowed = host in {"fcm.googleapis.com", "updates.push.services.mozilla.com"}
        allowed = (
            allowed or host == "web.push.apple.com" or host.endswith(".push.apple.com")
        )
        allowed = allowed or host.endswith(".notify.windows.com")
        if url.username is not None or url.password is not None:
            raise ValueError("Push endpoints cannot contain credentials.")
        if (
            url.scheme != "https"
            or not allowed
            or url.port not in {None, 443}
            or url.fragment
            or len(endpoint) > MAX_ENDPOINT_LENGTH
        ):
            raise ValueError("Unsupported web push endpoint.")
    except ValueError as error:
        raise ValueError("Unsupported web push endpoint.") from error
