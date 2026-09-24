from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from eastwatch import watcher


def assistant_line(
    *,
    input_tok,
    output_tok,
    cache_read,
    cache_write,
    reasoning,
    total,
    cost,
    content=None,
):
    return json.dumps(
        {
            "type": "message",
            "message": {
                "role": "assistant",
                "content": content if content is not None else [],
                "usage": {
                    "input": input_tok,
                    "output": output_tok,
                    "cacheRead": cache_read,
                    "cacheWrite": cache_write,
                    "reasoning": reasoning,
                    "totalTokens": total,
                    "cost": {"total": cost},
                },
            },
        }
    )


def synthetic_session_file(path: Path) -> Path:
    lines = [
        json.dumps({"type": "session", "id": "sess-1"}),
        assistant_line(
            input_tok=100,
            output_tok=50,
            cache_read=10,
            cache_write=5,
            reasoning=20,
            total=185,
            cost=0.01,
            content=[{"type": "toolCall", "name": "task"}],
        ),
        assistant_line(
            input_tok=200,
            output_tok=80,
            cache_read=15,
            cache_write=0,
            reasoning=30,
            total=325,
            cost=0.02,
        ),
        json.dumps({"type": "compaction"}),
        assistant_line(
            input_tok=300,
            output_tok=90,
            cache_read=20,
            cache_write=5,
            reasoning=10,
            total=425,
            cost=0.03,
        ),
    ]
    session_file = path / "session.jsonl"
    session_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return session_file


def fake_models_dir(path: Path, *, include_models_json: bool = True) -> Path:
    models_dir = path / "pi-agent"
    models_dir.mkdir(parents=True, exist_ok=True)
    if include_models_json:
        (models_dir / "models.json").write_text(
            json.dumps(
                {
                    "providers": {
                        "pi-headroom": {
                            "models": [
                                {"id": "gpt-5.6-sol", "contextWindow": 400000},
                                {"id": "other-model", "contextWindow": 123},
                            ]
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
    return models_dir


def fake_journal(path: Path, *, ts: float) -> Path:
    journal_file = path / "run.jsonl"
    journal_file.write_text(
        json.dumps({"v": 1, "ts": ts, "run_id": "run-1", "type": "run_started"}) + "\n",
        encoding="utf-8",
    )
    return journal_file


def test_compute_run_stats_aggregates_usage_compactions_and_subagents(
    tmp_path, monkeypatch
):
    session_file = synthetic_session_file(tmp_path)
    models_dir = fake_models_dir(tmp_path)
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(models_dir))
    journal = fake_journal(tmp_path, ts=time.time() - 252)

    stats = watcher.compute_run_stats(str(session_file), "gpt-5.6-sol", str(journal))

    assert stats is not None
    assert stats["schema"] == 1
    assert stats["turns"] == 3
    assert stats["input"] == 600
    assert stats["output"] == 220
    assert stats["cache_read"] == 45
    assert stats["cache_write"] == 10
    assert stats["reasoning"] == 60
    assert stats["total_tokens"] == 935
    assert stats["cost_usd"] == pytest.approx(0.06)
    assert stats["compactions"] == 1
    assert stats["subagents"] == 1
    assert stats["context_tokens"] == 325
    assert stats["context_window"] == 400000
    assert stats["duration_s"] is not None
    assert stats["duration_s"] >= 250


def test_compute_run_stats_corrupt_session_file_returns_none(tmp_path, monkeypatch):
    models_dir = fake_models_dir(tmp_path)
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(models_dir))

    stats = watcher.compute_run_stats(
        str(tmp_path / "missing-session.jsonl"), "gpt-5.6-sol", None
    )

    assert stats is None


def test_compute_run_stats_missing_models_json_leaves_context_window_none(
    tmp_path, monkeypatch
):
    session_file = synthetic_session_file(tmp_path)
    models_dir = fake_models_dir(tmp_path, include_models_json=False)
    monkeypatch.setenv("PI_CODING_AGENT_DIR", str(models_dir))

    stats = watcher.compute_run_stats(str(session_file), "gpt-5.6-sol", None)

    assert stats is not None
    assert stats["context_window"] is None
    assert stats["duration_s"] is None


def test_render_stats_footer_none_or_falsy_is_empty_string():
    assert watcher.render_stats_footer(None) == ""
    assert watcher.render_stats_footer({}) == ""


def test_render_stats_footer_renders_collapsed_table():
    stats = {
        "schema": 1,
        "duration_s": 252,
        "turns": 3,
        "input": 600,
        "output": 220,
        "cache_read": 45,
        "cache_write": 10,
        "reasoning": 60,
        "total_tokens": 935,
        "cost_usd": 0.06,
        "compactions": 1,
        "subagents": 1,
        "context_tokens": 325,
        "context_window": 400000,
    }

    footer = watcher.render_stats_footer(stats)

    assert "<details>" in footer
    assert "</details>" in footer
    assert "<br" not in footer
    assert "4m12s" in footer
    assert "935 tokens" in footer
    assert "325 / 400000 (0%)" in footer
    assert "| metric | value |" in footer
    assert "| compactions | 1 |" in footer
    assert "| subagent calls | 1 |" in footer


def test_render_stats_footer_uses_unknown_marker_when_total_tokens_missing():
    footer = watcher.render_stats_footer(
        {"schema": 1, "total_tokens": None, "turns": 0}
    )

    assert "? tokens" in footer
