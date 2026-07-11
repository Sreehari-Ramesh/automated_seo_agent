import sys
import types


class _FakeChatModel:
    def __init__(self, *args, **kwargs):
        pass

    async def ainvoke(self, *args, **kwargs):
        return types.SimpleNamespace(content="SAFE_TO_PROCEED")


if "langchain_anthropic" not in sys.modules:
    anthropic = types.ModuleType("langchain_anthropic")
    anthropic.ChatAnthropic = _FakeChatModel
    sys.modules["langchain_anthropic"] = anthropic

if "langchain_openai" not in sys.modules:
    openai = types.ModuleType("langchain_openai")
    openai.ChatOpenAI = _FakeChatModel
    sys.modules["langchain_openai"] = openai

from src.agents.fix_executor_agent import _apply_task, _split_dependency_batches


def _task(task_id: str, change_type: str, risk_level: str, wp_post_id: int) -> dict:
    return {
        "task_id": task_id,
        "page_url": f"https://example.com/{task_id}",
        "wp_post_id": wp_post_id,
        "post_type": "page",
        "change_type": change_type,
        "field_name": "content" if change_type == "content_rewrite" else "meta",
        "old_value": "old",
        "new_value": "<p>new</p>" if change_type == "content_rewrite" else "new",
        "risk_level": risk_level,
        "approved": True,
        "snapshot": None,
        "error": None,
        "applied": False,
    }


def test_high_risk_content_rewrites_are_isolated_batches():
    tasks = [
        _task("rewrite-1", "content_rewrite", "high", 1),
        _task("rewrite-2", "content_rewrite", "high", 2),
        _task("meta-1", "meta_title", "low", 3),
    ]

    batches, isolated = _split_dependency_batches(tasks)

    assert isolated is True
    assert [[task["task_id"] for task in batch] for batch in batches] == [
        ["rewrite-1"],
        ["rewrite-2"],
        ["meta-1"],
    ]


async def test_dry_run_mode_simulates_without_wordpress_writes():
    updated = await _apply_task(_task("rewrite-1", "content_rewrite", "high", 1), "dry_run", {})

    assert updated["applied"] is True
    assert updated["error"].startswith("[DRY_RUN]")
