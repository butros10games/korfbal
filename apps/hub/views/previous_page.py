from django.shortcuts import redirect
from django.http import HttpResponseRedirect
from django.db.models import Q

from apps.player.models import Player
from apps.hub.models import PageConnectRegistration


def previous_page(request):
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
    if len(unique_pages) > counter:
        referer = unique_pages[counter]
    else:
        referer = None

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
