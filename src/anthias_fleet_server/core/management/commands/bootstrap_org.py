"""One-time setup for a fresh Fleet Server deployment.

Referenced from ``OrganizationViewSet``'s docstring: an Organization
is provisioned once, outside the API, not created through it — this
command (or the Django admin) is that provisioning path.
"""

from __future__ import annotations

from typing import Any

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from anthias_fleet_server.core.models import Membership, Organization


class Command(BaseCommand):
    help = (
        'Create the first Organization (if none exists) and make the '
        'given user its Owner. Safe to re-run: an existing '
        'Organization is reused, and re-running for the same user '
        'just confirms/updates their role to Owner rather than '
        'erroring.'
    )

    def add_arguments(self, parser: Any) -> None:
        parser.add_argument(
            'username',
            help='Username of an existing user to make Owner.',
        )
        parser.add_argument(
            '--organization-name',
            default='Default',
            help=(
                'Name for the Organization if one does not exist yet '
                '(default: "Default"). Ignored if an Organization '
                'already exists — this command intentionally never '
                "creates a second one; see Organization's docstring "
                "on why multi-tenancy isn't a build target here."
            ),
        )

    @transaction.atomic
    def handle(self, *args: Any, **options: Any) -> None:
        User = get_user_model()
        try:
            user = User.objects.get(username=options['username'])
        except User.DoesNotExist as exc:
            raise CommandError(
                f'No such user: {options["username"]!r}. Create one '
                'first with `manage.py createsuperuser` or the admin.'
            ) from exc

        organization, created = Organization.objects.get_or_create(
            defaults={'name': options['organization_name']},
        )
        if created:
            self.stdout.write(f'Created organization {organization.name!r}.')
        else:
            self.stdout.write(
                f'Using existing organization {organization.name!r}.'
            )

        _membership, created = Membership.objects.update_or_create(
            user=user,
            organization=organization,
            defaults={'role': Membership.OWNER},
        )
        verb = 'Made' if created else 'Confirmed'
        self.stdout.write(
            self.style.SUCCESS(
                f'{verb} {user.username!r} Owner of {organization.name!r}.'
            )
        )
