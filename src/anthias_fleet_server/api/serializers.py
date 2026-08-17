from __future__ import annotations

from typing import Any, ClassVar

from rest_framework.serializers import ModelSerializer, ValidationError

from anthias_fleet_server.core.models import (
    Deployment,
    Group,
    Media,
    Membership,
    Organization,
    Player,
    Playlist,
    PlaylistItem,
)


class OrganizationSerializer(ModelSerializer[Organization]):
    class Meta:
        model = Organization
        fields: ClassVar = ['id', 'name', 'created_at']
        read_only_fields: ClassVar = ['id', 'created_at']


class MembershipSerializer(ModelSerializer[Membership]):
    class Meta:
        model = Membership
        fields: ClassVar = [
            'id',
            'user',
            'organization',
            'role',
            'created_at',
        ]
        read_only_fields: ClassVar = ['id', 'organization', 'created_at']


class GroupSerializer(ModelSerializer[Group]):
    class Meta:
        model = Group
        fields: ClassVar = ['id', 'organization', 'name', 'created_at']
        read_only_fields: ClassVar = ['id', 'organization', 'created_at']


class PlayerSerializer(ModelSerializer[Player]):
    class Meta:
        model = Player
        fields: ClassVar = [
            'id',
            'organization',
            'name',
            'device_id',
            'groups',
            'created_at',
        ]
        read_only_fields: ClassVar = ['id', 'organization', 'created_at']

    def validate_groups(self, groups: list[Group]) -> list[Group]:
        # get_organization() is only present when this serializer is
        # instantiated by a request-bound viewset (the normal case);
        # direct instantiation in a test without context skips this
        # check, matching how the queryset itself is unfiltered then.
        get_organization = self.context.get('get_organization')
        if get_organization is None:
            return groups
        organization = get_organization()
        foreign = [g for g in groups if g.organization_id != organization.id]
        if foreign:
            raise ValidationError(
                'One or more groups belong to a different organization.'
            )
        return groups


class MediaSerializer(ModelSerializer[Media]):
    """Read/list shape. Upload (create) goes through
    ``MediaUploadSerializer`` instead — ``size``/``checksum`` are
    server-computed from the uploaded bytes, never client-supplied.
    """

    class Meta:
        model = Media
        fields: ClassVar = [
            'id',
            'organization',
            'file',
            'filename',
            'mimetype',
            'size',
            'checksum',
            'metadata',
            'created_at',
        ]
        read_only_fields: ClassVar = fields


class MediaUploadSerializer(ModelSerializer[Media]):
    class Meta:
        model = Media
        fields: ClassVar = ['file', 'filename', 'metadata']
        extra_kwargs: ClassVar = {'filename': {'required': False}}

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        if not attrs.get('filename'):
            attrs['filename'] = attrs['file'].name
        return attrs


class PlaylistItemSerializer(ModelSerializer[PlaylistItem]):
    class Meta:
        model = PlaylistItem
        fields: ClassVar = [
            'id',
            'playlist',
            'media',
            'order',
            'duration',
            'play_days',
            'play_time_from',
            'play_time_to',
            'start_date',
            'end_date',
        ]
        read_only_fields: ClassVar = ['id']


class PlaylistSerializer(ModelSerializer[Playlist]):
    items = PlaylistItemSerializer(many=True, read_only=True)

    class Meta:
        model = Playlist
        fields: ClassVar = [
            'id',
            'organization',
            'name',
            'version',
            'items',
            'created_at',
            'updated_at',
        ]
        read_only_fields: ClassVar = [
            'id',
            'organization',
            'version',
            'created_at',
            'updated_at',
        ]


class DeploymentSerializer(ModelSerializer[Deployment]):
    class Meta:
        model = Deployment
        fields: ClassVar = [
            'id',
            'organization',
            'playlist',
            'player',
            'group',
            'version',
            'created_by',
            'created_at',
        ]
        read_only_fields: ClassVar = [
            'id',
            'organization',
            'version',
            'created_by',
            'created_at',
        ]

    def validate(self, attrs: dict[str, Any]) -> dict[str, Any]:
        player = attrs.get('player')
        group = attrs.get('group')
        if bool(player) == bool(group):
            raise ValidationError(
                'A deployment must target exactly one of player or group.'
            )
        return attrs
