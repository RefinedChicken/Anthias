from django.urls import path

from . import views

app_name = 'anthias_app'

# Every route declares a trailing slash so Django's APPEND_SLASH=True
# (default) handles the slashless variant for free: `/system-info`
# permanently redirects to `/system-info/` (HTTP 301 for GET, 308
# method-preserving for POST/PUT/etc.) instead of 404-ing. The earlier
# slashless convention worked for inbound `{% url %}` references but
# silently broke any external bookmark / share / copy-pasted link
# typed without the trailing slash. APPEND_SLASH only ADDS slashes,
# never removes them, so consistency-at-trailing-slash covers both
# directions.
urlpatterns = [
    path('splash-page/', views.splash_page, name='splash_page'),
    path('setup/', views.setup, name='setup'),
    path('login/', views.login, name='login'),
    path('', views.home, name='home'),
    path('system-info/', views.system_info, name='system_info'),
    path('integrations/', views.integrations, name='integrations'),
    path('settings/', views.settings_view, name='settings'),
    path('settings/save/', views.settings_save, name='settings_save'),
    path('settings/backup/', views.settings_backup, name='settings_backup'),
    path('settings/recover/', views.settings_recover, name='settings_recover'),
    path('settings/reboot/', views.settings_reboot, name='settings_reboot'),
    path(
        'settings/shutdown/',
        views.settings_shutdown,
        name='settings_shutdown',
    ),
    path(
        'settings/display/<str:state>/',
        views.settings_display_power,
        name='settings_display_power',
    ),
    path(
        'settings/api-tokens/create/',
        views.api_tokens_create,
        name='api_tokens_create',
    ),
    path(
        'settings/api-tokens/<int:token_id>/revoke/',
        views.api_tokens_revoke,
        name='api_tokens_revoke',
    ),
    path(
        'settings/fleet-pairing/create/',
        views.fleet_pairing_create,
        name='fleet_pairing_create',
    ),
    path(
        'settings/fleet-pairing/revoke/',
        views.fleet_pairing_revoke,
        name='fleet_pairing_revoke',
    ),
    path('settings/users/create/', views.user_create, name='user_create'),
    path(
        'settings/users/<int:user_id>/edit/',
        views.user_edit,
        name='user_edit',
    ),
    path(
        'settings/users/<int:user_id>/reset-password/',
        views.user_reset_password,
        name='user_reset_password',
    ),
    path(
        'settings/users/<int:user_id>/delete/',
        views.user_delete,
        name='user_delete',
    ),
    path(
        'settings/migrate-to-screenly/',
        views.migrate_to_screenly,
        name='migrate_to_screenly',
    ),
    path(
        'settings/import/<str:provider>/',
        views.import_content,
        name='import_content',
    ),
    path(
        '_partials/asset-table/',
        views.assets_table_partial,
        name='assets_table',
    ),
    path(
        'review-cta/dismiss/',
        views.review_cta_dismiss,
        name='review_cta_dismiss',
    ),
    path(
        'review-cta/snooze/',
        views.review_cta_snooze,
        name='review_cta_snooze',
    ),
    path('assets/new/', views.assets_create, name='assets_create'),
    # Path deliberately avoids the `assets/new` prefix: a test/selector
    # matching form[action*="assets/new"] would otherwise also match
    # this form's submit button.
    path('apps/install/', views.assets_create_app, name='assets_create_app'),
    path('assets/upload/', views.assets_upload, name='assets_upload'),
    path('assets/order/', views.assets_order, name='assets_order'),
    path(
        'assets/bulk/action/',
        views.assets_bulk_action,
        name='assets_bulk_action',
    ),
    path(
        'assets/bulk/update/',
        views.assets_bulk_update,
        name='assets_bulk_update',
    ),
    path(
        'assets/control/<str:command>/',
        views.assets_control,
        name='assets_control',
    ),
    path(
        'assets/<str:asset_id>/update/',
        views.assets_update,
        name='assets_update',
    ),
    path(
        'assets/<str:asset_id>/toggle/',
        views.assets_toggle,
        name='assets_toggle',
    ),
    path(
        'assets/<str:asset_id>/delete/',
        views.assets_delete,
        name='assets_delete',
    ),
    path(
        'assets/<str:asset_id>/download/',
        views.assets_download,
        name='assets_download',
    ),
    path(
        'assets/<str:asset_id>/preview/',
        views.assets_preview,
        name='assets_preview',
    ),
]
