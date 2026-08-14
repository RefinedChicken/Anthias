from django.urls import path

from . import views

app_name = 'anthias_fleet'

# Trailing slash on every route — same APPEND_SLASH=True rationale as
# anthias_server/app/urls.py.
urlpatterns = [
    path('', views.player_list, name='player_list'),
    path(
        '_partials/player-table/',
        views.player_table_partial,
        name='player_table',
    ),
    path('players/new/', views.player_new, name='player_new'),
    path(
        'players/bulk/action/',
        views.players_bulk_action,
        name='players_bulk_action',
    ),
    path(
        'players/<int:player_id>/', views.player_detail, name='player_detail'
    ),
    path(
        'players/<int:player_id>/edit/',
        views.player_edit,
        name='player_edit',
    ),
    path(
        'players/<int:player_id>/delete/',
        views.player_delete,
        name='player_delete',
    ),
    path(
        'players/<int:player_id>/assets/_partials/table/',
        views.player_assets_table_partial,
        name='player_assets_table',
    ),
    path(
        'players/<int:player_id>/assets/new/',
        views.player_asset_create,
        name='player_asset_create',
    ),
    path(
        'players/<int:player_id>/assets/upload/',
        views.player_asset_upload,
        name='player_asset_upload',
    ),
    path(
        'players/<int:player_id>/assets/apps/',
        views.player_asset_create_app,
        name='player_asset_create_app',
    ),
    path(
        'players/<int:player_id>/assets/bulk/action/',
        views.player_assets_bulk_action,
        name='player_assets_bulk_action',
    ),
    path(
        'players/<int:player_id>/assets/control/<str:command>/',
        views.player_asset_control,
        name='player_asset_control',
    ),
    path(
        'players/<int:player_id>/assets/<str:asset_id>/toggle/',
        views.player_asset_toggle,
        name='player_asset_toggle',
    ),
    path(
        'players/<int:player_id>/assets/<str:asset_id>/delete/',
        views.player_asset_delete,
        name='player_asset_delete',
    ),
    path(
        'players/<int:player_id>/assets/<str:asset_id>/move/<str:direction>/',
        views.player_asset_move,
        name='player_asset_move',
    ),
    path('groups/', views.group_list, name='group_list'),
    path('groups/new/', views.group_new, name='group_new'),
    path('groups/<int:group_id>/edit/', views.group_edit, name='group_edit'),
    path(
        'groups/<int:group_id>/delete/',
        views.group_delete,
        name='group_delete',
    ),
]
