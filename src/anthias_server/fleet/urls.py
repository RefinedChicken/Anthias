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
        'players/<int:player_id>/settings/',
        views.player_settings,
        name='player_settings',
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
    path(
        'players/<int:player_id>/assets/<str:asset_id>/push/',
        views.player_asset_push,
        name='player_asset_push',
    ),
    path(
        'push-jobs/<int:job_id>/',
        views.push_job_status,
        name='push_job_status',
    ),
    path(
        'push-jobs/<int:job_id>/_partials/status/',
        views.push_job_status_partial,
        name='push_job_status_partial',
    ),
    path('groups/', views.group_list, name='group_list'),
    path('groups/new/', views.group_new, name='group_new'),
    path('groups/<int:group_id>/edit/', views.group_edit, name='group_edit'),
    path(
        'groups/<int:group_id>/delete/',
        views.group_delete,
        name='group_delete',
    ),
    path('templates/', views.template_list, name='template_list'),
    path('templates/new/', views.template_new, name='template_new'),
    path(
        'templates/<int:template_id>/',
        views.template_detail,
        name='template_detail',
    ),
    path(
        'templates/<int:template_id>/edit/',
        views.template_edit,
        name='template_edit',
    ),
    path(
        'templates/<int:template_id>/delete/',
        views.template_delete,
        name='template_delete',
    ),
    path(
        'templates/<int:template_id>/apply/',
        views.template_apply,
        name='template_apply',
    ),
    path(
        'templates/<int:template_id>/items/new/',
        views.template_item_new,
        name='template_item_new',
    ),
    path(
        'templates/<int:template_id>/items/<int:item_id>/edit/',
        views.template_item_edit,
        name='template_item_edit',
    ),
    path(
        'templates/<int:template_id>/items/<int:item_id>/delete/',
        views.template_item_delete,
        name='template_item_delete',
    ),
    path(
        'templates/<int:template_id>/items/<int:item_id>/move/<str:direction>/',
        views.template_item_move,
        name='template_item_move',
    ),
    path(
        'template-jobs/<int:job_id>/',
        views.template_job_status,
        name='template_job_status',
    ),
    path(
        'template-jobs/<int:job_id>/_partials/status/',
        views.template_job_status_partial,
        name='template_job_status_partial',
    ),
]
