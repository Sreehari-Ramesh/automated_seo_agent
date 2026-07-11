"""Tests for WordPress client (using respx to mock HTTP calls)."""

import pytest
import respx
import httpx
from src.wordpress.client import WordPressClient


@pytest.fixture
def wp_client():
    return WordPressClient(
        base_url="https://test-site.com",
        username="admin",
        app_password="test pass word",
    )


@respx.mock
@pytest.mark.asyncio
async def test_check_connection_success(wp_client):
    respx.get("https://test-site.com/wp-json/wp/v2/users/me").mock(
        return_value=httpx.Response(200, json={"id": 1, "name": "admin"})
    )
    result = await wp_client.check_connection()
    assert result is True


@respx.mock
@pytest.mark.asyncio
async def test_check_connection_failure(wp_client):
    respx.get("https://test-site.com/wp-json/wp/v2/users/me").mock(
        return_value=httpx.Response(401, json={"code": "rest_not_logged_in"})
    )
    result = await wp_client.check_connection()
    assert result is False


@respx.mock
@pytest.mark.asyncio
async def test_list_posts(wp_client):
    mock_posts = [
        {"id": 1, "title": {"rendered": "Post 1"}, "link": "https://test-site.com/post-1", "type": "post"},
        {"id": 2, "title": {"rendered": "Post 2"}, "link": "https://test-site.com/post-2", "type": "post"},
    ]
    respx.get("https://test-site.com/wp-json/wp/v2/posts").mock(
        return_value=httpx.Response(200, json=mock_posts)
    )
    posts = await wp_client.list_posts(per_page=10)
    assert len(posts) == 2
    assert posts[0]["id"] == 1


@respx.mock
@pytest.mark.asyncio
async def test_update_post_meta(wp_client):
    respx.put("https://test-site.com/wp-json/wp/v2/posts/42").mock(
        return_value=httpx.Response(200, json={"id": 42, "meta": {"_yoast_wpseo_title": "New Title"}})
    )
    result = await wp_client.update_post_meta(42, {"_yoast_wpseo_title": "New Title"})
    assert result["id"] == 42


@respx.mock
@pytest.mark.asyncio
async def test_snapshot_post(wp_client):
    respx.get("https://test-site.com/wp-json/wp/v2/posts/5").mock(
        return_value=httpx.Response(200, json={
            "id": 5,
            "title": {"rendered": "Test Post"},
            "content": {"rendered": "<p>Content</p>"},
            "meta": {"_yoast_wpseo_title": "SEO Title"},
            "yoast_head_json": {"title": "SEO Title", "description": "SEO Desc"},
        })
    )
    snapshot = await wp_client.snapshot_post(5, "post")
    assert snapshot["wp_post_id"] == 5
    assert snapshot["title"] == "Test Post"
    assert "meta" in snapshot


@respx.mock
@pytest.mark.asyncio
async def test_check_ai_meta_support_success(wp_client):
    fields = {
        "ai_meta_title": {"type": "string"},
        "ai_meta_description": {"type": "string"},
        "ai_canonical_url": {"type": "string"},
        "ai_og_title": {"type": "string"},
        "ai_og_description": {"type": "string"},
        "ai_og_image": {"type": "string"},
        "ai_schema_json": {"type": "string"},
        "ai_focus_keyword": {"type": "string"},
    }
    respx.options("https://test-site.com/wp-json/wp/v2/pages/5").mock(
        return_value=httpx.Response(
            200,
            json={"schema": {"properties": {"meta": {"properties": fields}}}},
        )
    )

    result = await wp_client.check_ai_meta_support(5, "page")

    assert result["supported"] is True
    assert result["missing_fields"] == []


@respx.mock
@pytest.mark.asyncio
async def test_check_ai_meta_support_missing_fields(wp_client):
    respx.options("https://test-site.com/wp-json/wp/v2/posts/5").mock(
        return_value=httpx.Response(
            200,
            json={"schema": {"properties": {"meta": {"properties": {}}}}},
        )
    )

    result = await wp_client.check_ai_meta_support(5, "post")

    assert result["supported"] is False
    assert "ai_meta_title" in result["missing_fields"]
