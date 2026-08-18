"""Fleet API viewsets.

Every viewset (except ``OrganizationViewSet``, which is read-only —
see ``core.models.Organization``'s docstring on why org creation isn't
an API concern in this phase) scopes its queryset to the requesting
user's own organization and injects that organization on create, so a
client can never read or write another organization's rows even
though the schema is technically multi-tenant-ready.
"""

from __future__ import annotations

import hashlib
from typing import Any

from django.db import IntegrityError
from django.db.models import Q
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.request import Request
from rest_framework.response import Response

from anthias_fleet_server.api.permissions import HasMinimumRole, get_membership
from anthias_fleet_server.api.serializers import (
    DeploymentSerializer,
    GroupSerializer,
    MediaSerializer,
    MediaUploadSerializer,
    MembershipSerializer,
    OrganizationSerializer,
    PairingApproveSerializer,
    PairingRequestSerializer,
    PlayerSerializer,
    PlaylistItemSerializer,
    PlaylistSerializer,
)
from anthias_fleet_server.core.models import (
    Deployment,
    Group,
    Media,
    Membership,
    Organization,
    PairingRequest,
    Player,
    PlayerCredential,
    Playlist,
    PlaylistItem,
)


class OrganizationScopedViewSet(viewsets.ModelViewSet[Any]):
    """Base for every viewset below except ``OrganizationViewSet``."""

    permission_classes = (IsAuthenticated, HasMinimumRole)

    def get_organization(self) -> Organization:
        membership = get_membership(self.request)
        # Unreachable once HasMinimumRole has already denied a
        # membership-less request, but keeps this method safe to call
        # from anywhere (e.g. a serializer's validate_groups) without
        # re-deriving that guarantee.
        assert membership is not None
        return membership.organization

    def get_queryset(self) -> Any:
        return (
            super().get_queryset().filter(organization=self.get_organization())
        )

    def get_serializer_context(self) -> dict[str, Any]:
        context = dict(super().get_serializer_context())
        context['get_organization'] = self.get_organization
        return context

    def perform_create(self, serializer: Any) -> None:
        serializer.save(organization=self.get_organization())


class OrganizationViewSet(viewsets.ReadOnlyModelViewSet[Any]):
    """Read-only: an org is provisioned once (Django admin / a future
    setup command), not created through this API — see the model's
    docstring on why multi-tenancy isn't a build target here."""

    queryset = Organization.objects.all()
    serializer_class = OrganizationSerializer
    permission_classes = (IsAuthenticated, HasMinimumRole)

    def get_queryset(self) -> Any:
        membership = get_membership(self.request)
        if membership is None:
            return Organization.objects.none()
        return Organization.objects.filter(pk=membership.organization_id)


class MembershipViewSet(OrganizationScopedViewSet):
    queryset = Membership.objects.select_related('user', 'organization')
    serializer_class = MembershipSerializer
    required_role_write = Membership.ADMINISTRATOR


class GroupViewSet(OrganizationScopedViewSet):
    queryset = Group.objects.all()
    serializer_class = GroupSerializer


class PlayerViewSet(OrganizationScopedViewSet):
    queryset = Player.objects.prefetch_related('groups')
    serializer_class = PlayerSerializer

    @action(detail=True, methods=['post'], url_path='revoke-credential')
    def revoke_credential(
        self, request: Request, pk: str | None = None
    ) -> Response:
        """Revoke every currently-active device credential for this
        Player (plan §8 step 8 — "Fleet can revoke a PlayerCredential
        at any time"). The Player's next poll/call gets 401 and locally
        transitions its pairing to revoked; nothing here touches
        already-deployed content (unpairing's asset-authority handling
        is a later phase — see the plan's unpairing section)."""
        player = self.get_object()
        revoked = PlayerCredential.objects.filter(
            player=player, revoked_at__isnull=True
        ).update(revoked_at=timezone.now())
        return Response({'revoked': revoked})


class MediaViewSet(OrganizationScopedViewSet):
    queryset = Media.objects.all()
    serializer_class = MediaSerializer

    def get_serializer_class(self) -> Any:
        if self.action == 'create':
            return MediaUploadSerializer
        return MediaSerializer

    def create(self, request: Request, *args: Any, **kwargs: Any) -> Response:
        upload_serializer = MediaUploadSerializer(data=request.data)
        upload_serializer.is_valid(raise_exception=True)
        uploaded = upload_serializer.validated_data['file']
        filename = upload_serializer.validated_data['filename']
        metadata = upload_serializer.validated_data.get('metadata', {})

        checksum = hashlib.sha256(uploaded.read()).hexdigest()
        uploaded.seek(0)
        organization = self.get_organization()

        # Content-addressed "upload once": identical bytes already on
        # file for this organization return the existing row (200)
        # instead of duplicating storage — the unique_media_checksum_
        # per_org constraint is the backstop, this is the intended,
        # non-error path for a client re-uploading the same asset.
        existing = Media.objects.filter(
            organization=organization, checksum=checksum
        ).first()
        if existing is not None:
            return Response(
                MediaSerializer(existing).data, status=status.HTTP_200_OK
            )

        try:
            media = Media.objects.create(
                organization=organization,
                file=uploaded,
                filename=filename,
                mimetype=getattr(uploaded, 'content_type', '') or '',
                size=uploaded.size,
                checksum=checksum,
                metadata=metadata,
            )
        except IntegrityError:
            # Lost a create race against a concurrent identical
            # upload — the row that won is the one to return.
            media = Media.objects.get(
                organization=organization, checksum=checksum
            )
            return Response(
                MediaSerializer(media).data, status=status.HTTP_200_OK
            )

        return Response(
            MediaSerializer(media).data, status=status.HTTP_201_CREATED
        )


class PlaylistViewSet(OrganizationScopedViewSet):
    queryset = Playlist.objects.prefetch_related('items__media')
    serializer_class = PlaylistSerializer


class PlaylistItemViewSet(OrganizationScopedViewSet):
    """Nested under a playlist's org scope via the playlist FK itself
    (a PlaylistItem has no organization column of its own) — every
    write bumps the parent Playlist's version, which is what a
    Deployment snapshots and a Player's sync engine (a later phase)
    diffs against.
    """

    queryset = PlaylistItem.objects.select_related('playlist', 'media')
    serializer_class = PlaylistItemSerializer

    def get_queryset(self) -> Any:
        return self.queryset.filter(
            playlist__organization=self.get_organization()
        )

    def perform_create(self, serializer: Any) -> None:
        playlist = serializer.validated_data['playlist']
        if playlist.organization_id != self.get_organization().id:
            raise PermissionDenied(
                'Playlist belongs to a different organization.'
            )
        serializer.save()
        playlist.bump_version()

    def perform_update(self, serializer: Any) -> None:
        previous_playlist = serializer.instance.playlist
        new_playlist = serializer.validated_data.get(
            'playlist', previous_playlist
        )
        if new_playlist.organization_id != self.get_organization().id:
            raise PermissionDenied(
                'Playlist belongs to a different organization.'
            )
        serializer.save()
        new_playlist.bump_version()
        if new_playlist.pk != previous_playlist.pk:
            previous_playlist.bump_version()

    def perform_destroy(self, instance: PlaylistItem) -> None:
        playlist = instance.playlist
        instance.delete()
        playlist.bump_version()


class DeploymentViewSet(OrganizationScopedViewSet):
    queryset = Deployment.objects.select_related(
        'playlist', 'player', 'group', 'created_by'
    )
    serializer_class = DeploymentSerializer

    def perform_create(self, serializer: Any) -> None:
        playlist = serializer.validated_data['playlist']
        serializer.save(
            organization=self.get_organization(),
            version=playlist.version,
            created_by=self.request.user,
        )


class PairingRequestViewSet(viewsets.ReadOnlyModelViewSet[Any]):
    """Admin-facing view onto in-progress pairing handshakes
    (plan §8) — read-only plus the ``approve`` action below; a request
    is otherwise only ever created or transitioned by a Player's own
    poll (``api.pairing_views.PairingPollView``).

    Unlike every other Fleet resource, ``pending`` rows aren't scoped
    to an Organization yet — see ``PairingRequest``'s docstring for
    why ``organization`` starts out null. Every Operator+ member of
    the fleet's one Organization can see every pending request, which
    is safe only because multi-tenancy isn't a build target
    (``Organization``'s own docstring); ``approved``/``completed``
    rows *are* org-scoped, same as every other resource here.
    """

    queryset = PairingRequest.objects.select_related(
        'organization', 'player', 'approved_by'
    )
    serializer_class = PairingRequestSerializer
    permission_classes = (IsAuthenticated, HasMinimumRole)

    def get_queryset(self) -> Any:
        membership = get_membership(self.request)
        if membership is None:
            return PairingRequest.objects.none()
        return (
            super()
            .get_queryset()
            .filter(
                Q(status=PairingRequest.PENDING)
                | Q(organization=membership.organization)
            )
        )

    @action(detail=True, methods=['post'])
    def approve(self, request: Request, pk: str | None = None) -> Response:
        pairing_request = self.get_object()
        if pairing_request.status != PairingRequest.PENDING:
            raise PermissionDenied(
                'Only a pending pairing request can be approved.'
            )
        if pairing_request.expires_at <= timezone.now():
            raise PermissionDenied('This pairing request has expired.')

        body = PairingApproveSerializer(data=request.data)
        body.is_valid(raise_exception=True)
        membership = get_membership(request)
        assert membership is not None
        organization = membership.organization

        player_id = body.validated_data.get('player_id')
        name = body.validated_data.get('name')

        if player_id is not None:
            try:
                player = Player.objects.get(
                    pk=player_id, organization=organization
                )
            except Player.DoesNotExist as exc:
                raise PermissionDenied(
                    'No such player in this organization.'
                ) from exc
            if (
                player.device_id is not None
                and pairing_request.device_id is not None
                and player.device_id != pairing_request.device_id
            ):
                raise PermissionDenied(
                    'That player is already bound to a different device.'
                )
            if player.device_id is None:
                player.device_id = pairing_request.device_id
                player.save(update_fields=['device_id'])
        else:
            player = None
            if pairing_request.device_id is not None:
                player = Player.objects.filter(
                    organization=organization,
                    device_id=pairing_request.device_id,
                ).first()
            if player is None:
                player = Player.objects.create(
                    organization=organization,
                    name=(
                        name
                        or pairing_request.label
                        or f'Player {pairing_request.device_id}'
                    ),
                    device_id=pairing_request.device_id,
                )

        pairing_request.organization = organization
        pairing_request.player = player
        pairing_request.status = PairingRequest.APPROVED
        pairing_request.approved_at = timezone.now()
        pairing_request.approved_by = request.user
        pairing_request.save(
            update_fields=[
                'organization',
                'player',
                'status',
                'approved_at',
                'approved_by',
            ]
        )
        return Response(PairingRequestSerializer(pairing_request).data)
