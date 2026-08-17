from __future__ import annotations

import pytest
from django.contrib.auth.models import User
from django.core.management import call_command
from django.core.management.base import CommandError

from anthias_fleet_server.core.models import Membership, Organization

_PWD = 'fixture-bootstrap-pwd'  # NOSONAR


@pytest.mark.django_db
def test_bootstrap_org_creates_org_and_makes_owner() -> None:
    user = User.objects.create_user(username='alice', password=_PWD)

    call_command('bootstrap_org', 'alice')

    org = Organization.objects.get()
    assert org.name == 'Default'
    membership = Membership.objects.get(user=user, organization=org)
    assert membership.role == Membership.OWNER


@pytest.mark.django_db
def test_bootstrap_org_is_idempotent_and_reuses_existing_organization() -> (
    None
):
    User.objects.create_user(username='alice', password=_PWD)
    call_command('bootstrap_org', 'alice')

    call_command('bootstrap_org', 'alice')

    assert Organization.objects.count() == 1
    assert Membership.objects.count() == 1


@pytest.mark.django_db
def test_bootstrap_org_unknown_user_raises() -> None:
    with pytest.raises(CommandError):
        call_command('bootstrap_org', 'nobody')
