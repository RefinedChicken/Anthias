from rest_framework.routers import DefaultRouter

from anthias_fleet_server.api.views import (
    DeploymentViewSet,
    GroupViewSet,
    MediaViewSet,
    MembershipViewSet,
    OrganizationViewSet,
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

urlpatterns = router.urls
