"""RBAC for the Fleet API: Owner > Administrator > Operator > Viewer.

A view's *read* actions (GET/HEAD/OPTIONS) and *write* actions are
checked independently against ``required_role_read`` /
``required_role_write`` class attributes (default Viewer / Operator),
so "everyone on the team can look, only Operators+ can change
anything" is the default without every view repeating that split.
"""

from __future__ import annotations

from typing import ClassVar

from rest_framework.permissions import SAFE_METHODS, BasePermission
from rest_framework.request import Request
from rest_framework.views import APIView

from anthias_fleet_server.core.models import Membership

_ROLE_RANK: dict[str, int] = {
    Membership.VIEWER: 0,
    Membership.OPERATOR: 1,
    Membership.ADMINISTRATOR: 2,
    Membership.OWNER: 3,
}


def get_membership(request: Request) -> Membership | None:
    """The requesting user's Membership row.

    Single-org today (see ``core.models.Organization``'s docstring),
    so "the user's membership" is unambiguous — ``.first()`` rather
    than requiring the client to disambiguate an organization that
    doesn't yet exist as a concept anywhere else in this API.
    """
    user = request.user
    if user is None or not user.is_authenticated:
        return None
    return (
        Membership.objects.select_related('organization')
        .filter(user=user)
        .first()
    )


class HasMinimumRole(BasePermission):
    required_role_read: ClassVar[str] = Membership.VIEWER
    required_role_write: ClassVar[str] = Membership.OPERATOR

    def has_permission(self, request: Request, view: APIView) -> bool:
        membership = get_membership(request)
        if membership is None:
            return False
        required = (
            getattr(view, 'required_role_read', self.required_role_read)
            if request.method in SAFE_METHODS
            else getattr(view, 'required_role_write', self.required_role_write)
        )
        return _ROLE_RANK[membership.role] >= _ROLE_RANK[required]
