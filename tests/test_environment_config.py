"""Protocol credential and public site URL environment configuration."""

from __future__ import annotations

from typer.testing import CliRunner

from relay_detector import cli
from web import server


runner = CliRunner()


def _capture_detect(monkeypatch):
    captured = {}

    async def fake_run_detect(protocol, base_url, api_key, model, config, output):
        captured.update(
            protocol=protocol.value,
            base_url=base_url,
            api_key=api_key,
            model=model,
        )

    monkeypatch.setattr(cli, "_run_detect", fake_run_detect)
    return captured


def test_openai_detect_reads_openai_environment(monkeypatch):
    monkeypatch.setenv("OPENAI_BASE_URL", "https://openai-relay.example/v1")
    monkeypatch.setenv("OPENAI_API_KEY", "openai-test-key")
    monkeypatch.setenv("OPENAI_MODEL", "gpt-test-model")
    captured = _capture_detect(monkeypatch)

    result = runner.invoke(cli.app, ["detect", "--protocol", "openai"])

    assert result.exit_code == 0, result.output
    assert captured == {
        "protocol": "openai",
        "base_url": "https://openai-relay.example/v1",
        "api_key": "openai-test-key",
        "model": "gpt-test-model",
    }


def test_gemini_detect_reads_gemini_environment(monkeypatch):
    monkeypatch.setenv("GEMINI_BASE_URL", "https://gemini-relay.example/v1")
    monkeypatch.setenv("GEMINI_API_KEY", "gemini-test-key")
    monkeypatch.setenv("GEMINI_MODEL", "gemini-test-model")
    captured = _capture_detect(monkeypatch)

    result = runner.invoke(cli.app, ["detect", "--protocol", "gemini"])

    assert result.exit_code == 0, result.output
    assert captured == {
        "protocol": "gemini",
        "base_url": "https://gemini-relay.example/v1",
        "api_key": "gemini-test-key",
        "model": "gemini-test-model",
    }


def test_site_url_environment_is_normalized(monkeypatch):
    monkeypatch.setenv("VERIDROP_SITE_URL", " https://verify.example.com/base/ ")
    assert server._site_url_from_env() == "https://verify.example.com/base"


def test_result_template_uses_configured_report_url():
    template = server.templates.get_template("result.html")
    rendered = template.render(
        SITE_URL="https://verify.example.com",
        report_url="https://verify.example.com/r/report-123",
        job_id="report-123",
        report={
            "target_model": "gpt-test-model",
            "mode": "quick",
            "base_url": "https://upstream.example/v1",
            "protocol": "openai",
            "total_score": 100,
            "verdict": "passed",
        },
        rows=[],
        report_notes=[],
        breadcrumb_domain="upstream.example",
    )

    assert "由 <a href=\"https://verify.example.com/r/report-123\">" in rendered
    assert "https://verify.example.com/r/report-123.jpg" in rendered
    assert "由 <a href=\"https://upstream.example" not in rendered


def test_protocol_forms_match_claude_empty_field_behavior():
    cases = [
        ("index.html", "https://claude-relay.example", "claude-test-model"),
        ("openai.html", "https://openai-relay.example/v1", "gpt-test-model"),
        ("gemini.html", "https://gemini-relay.example/v1", "gemini-test-model"),
    ]

    for template_name, base_url, model in cases:
        rendered = server.templates.get_template(template_name).render(
            default_base_url=base_url,
            default_model=model,
            models=[],
        )
        assert f'placeholder="{base_url}"' in rendered
        assert f'placeholder="{model}"' in rendered
        assert f'value="{base_url}"' not in rendered
        assert f'value="{model}"' not in rendered


def test_openai_helper_does_not_show_example_domain():
    rendered = server.templates.get_template("openai.html").render(
        default_base_url="https://relay.example/v1",
        default_model="gpt-test-model",
        models=[],
    )
    hint = rendered.split('<p class="hint">', 1)[1].split("</p>", 1)[0]
    assert "api.example.com" not in hint


def test_forms_do_not_render_example_address_when_no_environment_default():
    for template_name in ("index.html", "openai.html", "gemini.html"):
        rendered = server.templates.get_template(template_name).render(
            default_base_url="",
            default_model="",
            models=[],
        )
        assert "api.example.com" not in rendered
        assert "请输入中转站接口地址" in rendered


def test_openai_and_gemini_allow_environment_backed_fields_to_be_empty():
    for template_name in ("openai.html", "gemini.html"):
        rendered = server.templates.get_template(template_name).render(
            default_base_url="",
            default_model="",
            models=[],
        )
        assert 'id="base_url" name="base_url" required' not in rendered
        assert 'id="model" name="model" required' not in rendered
        assert 'id="api_key" name="api_key" required' in rendered