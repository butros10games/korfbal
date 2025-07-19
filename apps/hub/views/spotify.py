"""Spotify OAuth2.0 flow and token refresh."""

from datetime import timedelta

from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.contrib.auth.models import User
from django.http import HttpRequest, HttpResponseRedirect
from django.shortcuts import redirect
from django.utils.timezone import now
import requests

from apps.player.models import SpotifyToken


HTTP_STATUS_OK = 200
SPOTIFY_CLIENT_ID = settings.SPOTIFY_CLIENT_ID
SPOTIFY_CLIENT_SECRET = settings.SPOTIFY_CLIENT_SECRET
SPOTIFY_REDIRECT_URI = settings.SPOTIFY_REDIRECT_URI


@login_required
def spotify_callback(request: HttpRequest) -> HttpResponseRedirect | None:
    """Handle Spotify OAuth callback and save tokens.

    Args:
        request (HttpRequest): The HTTP request object.

    Returns:
        HttpResponseRedirect: Redirects to the home page or an error page.

    """
    # Get authorization code from the request
    code = request.GET.get("code")

    if not code:
        return redirect("/")  # Handle error case (e.g., user denied access)

    # Exchange authorization code for access token
    token_url = "https://accounts.spotify.com/api/token"  # noqa S106
    response = requests.post(
        token_url,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": SPOTIFY_REDIRECT_URI,
            "client_id": SPOTIFY_CLIENT_ID,
            "client_secret": SPOTIFY_CLIENT_SECRET,
        },
        timeout=10,
    )

    if response.status_code != HTTP_STATUS_OK:
        return redirect("/")  # Handle failure

    data = response.json()

    access_token = data["access_token"]
    refresh_token = data["refresh_token"]
    expires_in = data["expires_in"]  # Time in seconds
    expires_at = now() + timedelta(seconds=expires_in)

    # Get user info from Spotify API
    user_info_url = "https://api.spotify.com/v1/me"
    headers = {"Authorization": f"Bearer {access_token}"}
    user_info = requests.get(user_info_url, headers=headers, timeout=10).json()
    spotify_user_id = user_info["id"]

    # Save or update user's Spotify token
    SpotifyToken.objects.update_or_create(
        user=request.user,
        defaults={
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_at": expires_at,
            "spotify_user_id": spotify_user_id,
        },
    )

    return redirect("/")


def refresh_spotify_token(user: User) -> None:
    """Refresh user's Spotify access token if expired."""
    spotify_token = SpotifyToken.objects.get(user=user)

    if spotify_token.expires_at < now():
        token_url = "https://accounts.spotify.com/api/token"  # noqa S106
        response = requests.post(
            token_url,
            data={
                "grant_type": "refresh_token",
                "refresh_token": spotify_token.refresh_token,
                "client_id": SPOTIFY_CLIENT_ID,
                "client_secret": SPOTIFY_CLIENT_SECRET,
            },
            timeout=10,
        )
        if response.status_code == HTTP_STATUS_OK:
            data = response.json()
            spotify_token.access_token = data["access_token"]
            spotify_token.expires_at = now() + timedelta(seconds=data["expires_in"])
            spotify_token.save()
        else:
            print("Failed to refresh token")
