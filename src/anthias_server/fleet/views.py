from django.http import HttpRequest, HttpResponse


def player_list(request: HttpRequest) -> HttpResponse:
    # Placeholder so ANTHIAS_SERVICE=fleet has a working '/' route to
    # verify the app boots/migrates cleanly. Replaced by the real
    # player list/grid view.
    return HttpResponse('Anthias fleet server placeholder')
