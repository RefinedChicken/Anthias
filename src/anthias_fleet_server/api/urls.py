from django.urls import path
from rest_framework.routers import DefaultRouter

from anthias_fleet_server.api.pairing_views import (
    PairingAckView,
    PairingPollView,
)
from anthias_fleet_server.api.views import (
    DeploymentViewSet,
    GroupViewSet,
    MediaViewSet,
    MembershipViewSet,
    OrganizationViewSet,
    PairingRequestViewSet,
    PlayerViewSet,
    PlaylistItemViewSet,
    PlaylistViewSet,
)

router = DefaultRouter()
router.register('organizations', OrganizationViewSet, basename='organization')
router.register('memberships', MembershipViewSet, basename='membership')
router.register('groups', GroupViewSet, basename='group')
router.register('players', PlayerViewSet, basename='player')
router.register('media', MediaViewSet, basename='media')
router.register('playlists', PlaylistViewSet, basename='playlist')
router.register('playlist-items', PlaylistItemViewSet, basename='playlistitem')
router.register('deployments', DeploymentViewSet, basename='deployment')
router.register(
    'pairing-requests', PairingRequestViewSet, basename='pairingrequest'
)

urlpatterns = [
    # Unauthenticated (poll) / device-credential-authenticated (ack)
    # pairing-protocol endpoints — deliberately outside the DRF
    # router above, which is entirely human-RBAC-scoped. See
    # api.pairing_views' module docstring.
    path('pairing/poll', PairingPollView.as_view(), name='pairing_poll'),
    path('pairing/ack', PairingAckView.as_view(), name='pairing_ack'),
    *router.urls,
]
