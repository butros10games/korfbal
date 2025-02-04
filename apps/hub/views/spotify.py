"""Spotify OAuth2.0 flow and token refresh."""

import requests
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.shortcuts import redirect
from django.utils.timezone import now, timedelta

from apps.player.models import Player, SpotifyToken

SPOTIFY_CLIENT_ID = settings.SPOTIFY_CLIENT_ID
SPOTIFY_CLIENT_SECRET = settings.SPOTIFY_CLIENT_SECRET
SPOTIFY_REDIRECT_URI = settings.SPOTIFY_REDIRECT_URI


@login_required
def spotify_callback(request):
    """Handle Spotify OAuth callback and save tokens."""
    # Get authorization code from the request
    code = request.GET.get("code")

    if not code:
        return redirect("/")  # Handle error case (e.g., user denied access)

    # Exchange authorization code for access token
    token_url = "https://accounts.spotify.com/api/token"
    response = requests.post(
        token_url,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": SPOTIFY_REDIRECT_URI,
            "client_id": SPOTIFY_CLIENT_ID,
            "client_secret": SPOTIFY_CLIENT_SECRET,
        },
    )

    if response.status_code != 200:
        return redirect("/")  # Handle failure

    data = response.json()

    access_token = data["access_token"]
    refresh_token = data["refresh_token"]
    expires_in = data["expires_in"]  # Time in seconds
    expires_at = now() + timedelta(seconds=expires_in)

    # Get user info from Spotify API
    user_info_url = "https://api.spotify.com/v1/me"
    headers = {"Authorization": f"Bearer {access_token}"}
    user_info = requests.get(user_info_url, headers=headers).json()
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


def refresh_spotify_token(user):
    """Refresh user's Spotify access token if expired."""
    spotify_token = SpotifyToken.objects.get(user=user)

    if spotify_token.expires_at < now():
        token_url = "https://accounts.spotify.com/api/token"
        response = requests.post(
            token_url,
            data={
                "grant_type": "refresh_token",
                "refresh_token": spotify_token.refresh_token,
                "client_id": SPOTIFY_CLIENT_ID,
                "client_secret": SPOTIFY_CLIENT_SECRET,
            },
        )

        if response.status_code == 200:
            data = response.json()
            spotify_token.access_token = data["access_token"]
            spotify_token.expires_at = now() + timedelta(seconds=data["expires_in"])
            spotify_token.save()
        else:
            print("Failed to refresh token")
