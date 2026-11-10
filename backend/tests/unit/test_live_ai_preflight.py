"""Live verification must stop at unavailable prerequisites without changing the stack."""

import json
import sys
from pathlib import Path
from typing import Any

import httpx
import pytest
from dotenv import dotenv_values
from scripts import live_ai_check

from app.models.knowledge_chunk import EMBEDDING_DIMENSIONS

pytestmark = pytest.mark.unit

API_KEY = "sk-preflight-test-must-not-appear-in-reports"
VALUES = {
    "AI_PROVIDER": "groq",
    "AI_MODEL": "openai/gpt-oss-120b",
    "AI_API_KEY": "generation-test-key",
    "EMBEDDING_PROVIDER": "openai",
    "EMBEDDING_MODEL": "text-embedding-3-small",
    "EMBEDDING_API_KEY": API_KEY,
}


def response(payload: object, status: int = 200) -> httpx.Response:
    return httpx.Response(status, content=json.dumps(payload).encode())


def success() -> dict[str, Any]:
    return {
        "data": [{"index": 0, "embedding": [0.1] * EMBEDDING_DIMENSIONS}],
        "usage": {"prompt_tokens": 5},
    }


def transport(
    monkeypatch: pytest.MonkeyPatch, reply: httpx.Response | type[httpx.HTTPError]
) -> list[httpx.Request]:
    requests: list[httpx.Request] = []
    real_client = httpx.Client

    def handle(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if isinstance(reply, type):
            raise reply(API_KEY, request=request)
        return reply

    monkeypatch.setattr(
        live_ai_check.httpx,
        "Client",
        lambda **kwargs: real_client(transport=httpx.MockTransport(handle), **kwargs),
    )
    return requests


@pytest.fixture
def local_project(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Path:
    (tmp_path / "backend").mkdir()
    (tmp_path / ".env.production").write_text(
        "\n".join(
            f"{key}={value if not key.endswith('API_KEY') else ''}" for key, value in VALUES.items()
        ),
        encoding="utf-8",
    )
    (tmp_path / ".env").write_text(
        "\n".join(f"{key}={value}" for key, value in VALUES.items()), encoding="utf-8"
    )
    monkeypatch.setattr(live_ai_check, "ROOT", tmp_path)
    monkeypatch.setattr(live_ai_check, "TEMP_ENV", tmp_path / ".env.ai-checks.local")
    monkeypatch.setattr(sys, "argv", ["live_ai_check"])
    return tmp_path


@pytest.mark.parametrize("code", ["insufficient_quota", "credit_balance_exhausted"])
def test_quota_requires_credits_and_never_retries(
    monkeypatch: pytest.MonkeyPatch, code: str
) -> None:
    requests = transport(
        monkeypatch,
        response({"error": {"code": code, "message": API_KEY}}, 429),
    )
    result = live_ai_check.embedding_preflight(VALUES)

    assert not result["available"]
    assert result["http_status"] == 429
    assert result["error_code"] == code
    assert "credits" in result["reason"]
    assert API_KEY not in json.dumps(result)
    assert len(requests) == 1


def test_success_matches_the_application_request_and_column_width(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    requests = transport(monkeypatch, response(success()))
    result = live_ai_check.embedding_preflight(VALUES)

    assert result["available"]
    assert result["embedding_dimensions"] == EMBEDDING_DIMENSIONS
    assert result["prompt_tokens"] == 5
    assert len(requests) == 1
    assert str(requests[0].url) == "https://api.openai.com/v1/embeddings"
    assert requests[0].headers["Authorization"] == f"Bearer {API_KEY}"
    assert json.loads(requests[0].content) == {
        "model": VALUES["EMBEDDING_MODEL"],
        "input": ["SupportFlow embedding access check"],
    }
    assert "vectors" not in result
    assert API_KEY not in json.dumps(result)


@pytest.mark.parametrize(
    "payload",
    [
        [],
        {"data": []},
        {"data": [success()["data"][0]] * 2},
        {"data": [{"index": 4, "embedding": [0.1] * EMBEDDING_DIMENSIONS}]},
        {"data": [{"index": 0, "embedding": [0.1, 0.2]}]},
        {"data": [{"index": 0, "embedding": [API_KEY] * EMBEDDING_DIMENSIONS}]},
        {"data": [{"index": 0, "embedding": [float("nan")] * EMBEDDING_DIMENSIONS}]},
        {"data": [{"index": 0, "embedding": "encoded-vector"}]},
    ],
)
def test_malformed_success_is_not_ready(monkeypatch: pytest.MonkeyPatch, payload: object) -> None:
    requests = transport(monkeypatch, response(payload))
    result = live_ai_check.embedding_preflight(VALUES)

    assert not result["available"]
    assert "invalid response" in result["reason"]
    assert API_KEY not in json.dumps(result)
    assert len(requests) == 1


@pytest.mark.parametrize(
    "reply",
    [
        httpx.Response(502, content=API_KEY.encode()),
        response({"error": {"code": API_KEY, "message": API_KEY}}, 429),
        httpx.ReadTimeout,
        httpx.ConnectError,
    ],
)
def test_unknown_errors_and_transport_failures_do_not_leak_provider_text(
    monkeypatch: pytest.MonkeyPatch, reply: httpx.Response | type[httpx.HTTPError]
) -> None:
    requests = transport(monkeypatch, reply)
    result = live_ai_check.embedding_preflight(VALUES)

    assert not result["available"]
    assert result["error_code"] is None
    assert API_KEY not in json.dumps(result)
    assert len(requests) == 1


def test_full_run_stops_before_docker_and_preserves_previous_evidence(
    monkeypatch: pytest.MonkeyPatch, local_project: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    previous_report = local_project / "backend" / ".live-ai-rag-report.local"
    previous_report.write_text("previous RAG evidence", encoding="utf-8")
    original = {name: (local_project / name).read_bytes() for name in (".env", ".env.production")}
    requests = transport(
        monkeypatch,
        response({"error": {"code": "credit_balance_exhausted", "message": API_KEY}}, 429),
    )
    monkeypatch.setattr(live_ai_check, "docker", lambda *args: pytest.fail("Docker must not run"))

    assert live_ai_check.main() == 1

    report = json.loads(
        (local_project / "backend" / ".embedding-preflight-report.local").read_text()
    )
    assert report["scope"] == "embedding_preflight"
    assert report["failed"] == 1
    assert report["embedding_preflight"]["error_code"] == "credit_balance_exhausted"
    assert previous_report.read_text() == "previous RAG evidence"
    assert not live_ai_check.TEMP_ENV.exists()
    assert all(
        (local_project / name).read_bytes() == contents for name, contents in original.items()
    )
    assert API_KEY not in json.dumps(report) + capsys.readouterr().out
    assert len(requests) == 1


def test_preflight_only_needs_no_generation_key_or_docker(
    monkeypatch: pytest.MonkeyPatch, local_project: Path
) -> None:
    (local_project / ".env").write_text(
        "\n".join(
            f"{key}={value}" for key, value in VALUES.items() if key.startswith("EMBEDDING_")
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(sys, "argv", ["live_ai_check", "--preflight-only"])
    monkeypatch.setattr(live_ai_check, "docker", lambda *args: pytest.fail("Docker must not run"))
    requests = transport(monkeypatch, response(success()))

    assert live_ai_check.main() == 0
    assert not live_ai_check.TEMP_ENV.exists()
    assert len(requests) == 1
    assert not (local_project / "backend" / ".live-ai-rag-report.local").exists()


def test_generation_only_skips_preflight_and_stopped_docker_creates_no_secret_file(
    monkeypatch: pytest.MonkeyPatch, local_project: Path
) -> None:
    monkeypatch.setattr(sys, "argv", ["live_ai_check", "--generation-only"])
    monkeypatch.setattr(
        live_ai_check, "embedding_preflight", lambda values: pytest.fail("Embeddings must not run")
    )
    commands: list[tuple[str, ...]] = []

    def stopped_docker(*args: str) -> str:
        commands.append(args)
        raise RuntimeError("Docker command failed with exit code 1")

    monkeypatch.setattr(live_ai_check, "docker", stopped_docker)
    assert live_ai_check.main() == 1
    assert len(commands) == 1
    assert commands[0][0] == "info"
    assert not live_ai_check.TEMP_ENV.exists()


def test_successful_preflight_continues_full_checks_and_restores_configuration(
    monkeypatch: pytest.MonkeyPatch, local_project: Path
) -> None:
    requests = transport(monkeypatch, response(success()))
    original = dotenv_values(local_project / ".env.production")
    commands: list[tuple[str, ...]] = []

    def docker(*args: str) -> str:
        commands.append(args)
        if args[0] == "exec":
            return json.dumps(
                {"provider": VALUES["AI_PROVIDER"], "model": VALUES["AI_MODEL"], "has_key": True}
            )
        if args[0] == "inspect":
            return json.dumps(
                [{"Config": {"Env": [f"{key}={value}" for key, value in original.items()]}}]
            )
        if args[-3:] == ("config", "--format", "json"):
            return json.dumps(
                {"services": {name: {"environment": original} for name in ("backend", "worker")}}
            )
        return ""

    def checks(
        e: live_ai_check.Evidence, client: httpx.Client, values: dict[str, str], only: bool
    ) -> None:
        assert not only
        assert live_ai_check.TEMP_ENV.exists()
        assert all(
            dotenv_values(live_ai_check.TEMP_ENV)[key] == value for key, value in values.items()
        )
        e.details["scope"] = "generation_and_rag"
        e.check("full workflow invoked", True)

    monkeypatch.setattr(live_ai_check, "docker", docker)
    monkeypatch.setattr(live_ai_check.ssl, "create_default_context", lambda **kwargs: False)
    monkeypatch.setattr(live_ai_check, "wait_ready", lambda context: None)
    monkeypatch.setattr(live_ai_check, "run_checks", checks)

    assert live_ai_check.main() == 0

    report = json.loads((local_project / "backend" / ".live-ai-rag-report.local").read_text())
    assert report["scope"] == "generation_and_rag"
    assert report["failed"] == 0
    assert report["embedding_preflight"]["available"]
    assert not live_ai_check.TEMP_ENV.exists()
    assert dotenv_values(local_project / ".env.production") == original
    assert len(requests) == 1
    assert commands[0][0] == "info"
