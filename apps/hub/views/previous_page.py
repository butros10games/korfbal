"""Module contains the view for navigating back to the previous page."""

from django.db.models import Q
from django.http import HttpRequest, HttpResponseRedirect
from django.shortcuts import redirect

from apps.hub.models import PageConnectRegistration
from apps.player.models import Player


def previous_page(request: HttpRequest) -> HttpResponseRedirect:
    """View for navigating back to the previous page.

    Args:
        request (HttpRequest): The request object.

    Returns:
        HttpResponseRedirect: The response object.

    """
    player = Player.objects.get(user=request.user)

    # Get the counter from the URL or fallback to the session
    counter = request.GET.get("counter", request.session.get("back_counter", 1))
    try:
        counter = int(counter)  # Ensure the counter is an integer
    except ValueError:
        counter = 1  # Fallback to 1 if there"s any issue with the value

    pages = (
        PageConnectRegistration.objects.filter(player=player)
        .order_by("-registration_date")
        .exclude(
            Q(page__icontains="admin")
            | Q(page__icontains="selector")
            | Q(page__icontains="previous")
            | Q(page__icontains="api")
            | Q(page__icontains="login")
            | Q(page__icontains="logout")
            | Q(page__icontains="register")
            | Q(page__icontains="favicon.ico")
        )
        .values_list("page", flat=True)
    )

    # Remove consecutive duplicate URLs
    unique_pages = []
    previous_page = None
    for page in pages:
        if page != previous_page:
            unique_pages.append(page)
        previous_page = page

    # Adjust the counter to skip the most recent page
    referer = unique_pages[counter] if len(unique_pages) > counter else None

    # Update the session counter to allow for further back navigation
    request.session["back_counter"] = counter + 1
    request.session["is_back_navigation"] = True
    request.session.modified = True  # Save the session changes

    if referer:
        return HttpResponseRedirect(referer)
    else:
        request.session["back_counter"] = 1
        request.session.modified = True
        return redirect("catalog")
