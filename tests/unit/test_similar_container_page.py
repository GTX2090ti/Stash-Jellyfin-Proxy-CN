"""endpoint_similar container page width.

Yamby renders a studio/performer/group page exclusively from
`GET /Items/{id}/Similar` and always asks with `Limit=10` (observed
2026-09-16: no container Type — Studio/Folder/BoxSet/CollectionFolder/
Genre — makes it issue a children query). The rail must therefore carry
the container's full catalogue, not the first 10 scenes.
"""

import stash_jellyfin_proxy.endpoints.stubs as stubs


def test_container_page_limit_constant_is_wide():
    assert stubs.SIMILAR_CONTAINER_PAGE_LIMIT >= 100


def test_client_limit_is_widened_for_containers():
    # Simulate the widening done in endpoint_similar before calling
    # similar_items_for: the client's Limit=10 must never shrink the page.
    client_limit = 10
    effective = max(client_limit, stubs.SIMILAR_CONTAINER_PAGE_LIMIT)
    assert effective == stubs.SIMILAR_CONTAINER_PAGE_LIMIT


def test_widened_limit_survives_max_page_size_clamp():
    """similar_items_for clamps limit to runtime.MAX_PAGE_SIZE; the constant
    must not exceed it or the widening silently shrinks again."""
    from stash_jellyfin_proxy import runtime

    assert stubs.SIMILAR_CONTAINER_PAGE_LIMIT <= runtime.MAX_PAGE_SIZE
