from __future__ import annotations

import json

from hki.paths import resolve_scope
from hki.redaction import redact_text
from hki.timeline import build_timeline


def test_rtsp_url_embedded_password_is_redacted():
    result = redact_text("rtsp://admin:secret123@192.168.0.201:554/live")

    assert "secret123" not in result.text
    assert "rtsp://admin:[REDACTED_PASSWORD]@192.168.0.201:554/live" in result.text
    assert result.summary.counts["url_password"] == 1


def test_password_assignment_is_redacted():
    result = redact_text("password = super-secret")

    assert "super-secret" not in result.text
    assert "password = [REDACTED_PASSWORD]" in result.text
    assert result.summary.counts["password"] == 1


def test_wireguard_private_key_is_redacted():
    result = redact_text("PrivateKey = abcdefghijklmnopqrstuvwxyz0123456789ABCDEF=")

    assert "abcdefghijklmnopqrstuvwxyz" not in result.text
    assert "PrivateKey = [REDACTED_PRIVATE_KEY]" in result.text
    assert result.summary.counts["wireguard_private_key"] == 1


def test_api_and_token_values_are_redacted():
    result = redact_text("OPENAI_API_KEY=sk-abcdefghijklmnopqrstuvwx\nAuthorization: Bearer abcdefghijklmnopqrstuvwxyz")

    assert "sk-abcdefghijklmnopqrstuvwx" not in result.text
    assert "abcdefghijklmnopqrstuvwxyz" not in result.text
    assert result.text.count("[REDACTED_TOKEN]") == 2
    assert result.summary.counts["token"] == 2


def test_public_wireguard_key_is_not_redacted_without_private_label():
    public_key = "abcdefghijklmnopqrstuvwxyz0123456789ABCDEF="
    result = redact_text(f"PublicKey = {public_key}")

    assert public_key in result.text
    assert not result.summary.has_findings


def test_generated_timeline_report_and_json_are_redacted_without_modifying_source(tmp_path):
    source = tmp_path / "camera.md"
    source.write_text(
        "2025-01-01 camera rtsp://admin:secret123@192.168.0.201:554/live\n"
        "password = router-secret\n"
        "PrivateKey = abcdefghijklmnopqrstuvwxyz0123456789ABCDEF=\n",
        encoding="utf-8",
    )

    run = build_timeline(resolve_scope(tmp_path), "camera password PrivateKey")
    report = run.report_path.read_text(encoding="utf-8")
    data = json.loads(run.json_path.read_text(encoding="utf-8"))

    assert "secret123" not in report
    assert "router-secret" not in report
    assert "abcdefghijklmnopqrstuvwxyz0123456789ABCDEF" not in report
    assert "[REDACTED_PASSWORD]" in report
    assert "[REDACTED_PRIVATE_KEY]" in report
    assert "## Sensitivity Summary" in report
    assert "`url_password`" in report
    assert data["sensitivity"]["redactions_applied"] >= 3
    assert "secret123" not in json.dumps(data)
    assert "router-secret" not in json.dumps(data)
    assert "secret123" in source.read_text(encoding="utf-8")
    assert "router-secret" in source.read_text(encoding="utf-8")
