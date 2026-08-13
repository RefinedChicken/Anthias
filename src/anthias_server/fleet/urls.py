from django.urls import path

from . import views

app_name = 'anthias_fleet'

urlpatterns = [
    path('', views.player_list, name='player_list'),
]
