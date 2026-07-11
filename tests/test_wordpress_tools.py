from src.tools import wordpress_tools


class _FakeClient:
    async def update_page_meta(self, wp_post_id, meta):
        return {"id": wp_post_id, "meta": {}}

    async def update_post_meta(self, wp_post_id, meta):
        return {"id": wp_post_id, "meta": {}}


async def test_wp_update_seo_meta_reports_unpersisted_rest_meta(monkeypatch):
    monkeypatch.setattr(wordpress_tools, "_client", lambda: _FakeClient())

    result = await wordpress_tools.wp_update_seo_meta.ainvoke(
        {
            "wp_post_id": 10,
            "post_type": "page",
            "seo_title": "New SEO Title",
        }
    )

    assert result["success"] is False
    assert "did not persist" in result["error"]
    assert "seo-agent-meta.php" in result["error"]
