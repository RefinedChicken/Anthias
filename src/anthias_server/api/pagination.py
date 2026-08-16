from rest_framework.pagination import PageNumberPagination


class DefaultV2Pagination(PageNumberPagination):
    """Shared pagination for new, additive v2 list endpoints.

    Never applied to the pre-existing ``/api/v2/assets`` (or any v1/
    v1.1/v1.2 endpoint) — those keep returning a bare array; changing
    that shape would break the "never break the API" contract (see
    CLAUDE.md's API-versioning note). Every *new* list endpoint added
    for the Player/Fleet-Server work uses this from day one instead,
    per the plan's pagination-and-throttling-from-day-one rule.
    """

    page_size = 50
    page_size_query_param = 'page_size'
    max_page_size = 200
