from django.shortcuts import redirect
from django.http import HttpResponseRedirect

from apps.player.models import Player
from apps.hub.models import PageConnectRegistration


def previous_page(request):
    player = Player.objects.get(user=request.user)
    counter = request.session.get('back_counter', 1)
    pages = PageConnectRegistration.objects.filter(player=player).order_by('-registration_date').exclude(page='')

    if pages.count() > counter:
        referer = pages[counter].page
    else:
        referer = None

    request.session['back_counter'] = counter + 1
    request.session['is_back_navigation'] = True  # Set the flag

    if referer:
        return HttpResponseRedirect(referer)
    else:
        request.session['back_counter'] = 1
        return redirect('teams')
