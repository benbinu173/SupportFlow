"""Verify live generation and RAG through the local production stack, then restore it.

Run from backend: .venv/Scripts/python.exe -m scripts.live_ai_check
Use --generation-only while awaiting embedding access, or --preflight-only to check it
without Docker or a generation key. Full runs check embedding access before deployment.
Keys are read from the
gitignored .env.production or .env, never arguments or terminal output. A temporary,
gitignored production env file enables providers only for this run. Test accounts and
AI spend history remain; generated tickets are closed and knowledge documents deleted.
"""

# ruff: noqa: T201

import argparse
import json
import math
import ssl
import sys
import time
import uuid
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, cast

import httpx
from dotenv import dotenv_values, set_key
from pydantic import ValidationError

from app.models.knowledge_chunk import EMBEDDING_DIMENSIONS
from app.schemas.knowledge import Embedding
from scripts.phase_x_walkthrough import (
    ANSWERED_QUESTION,
    MARKDOWN,
    NO_ANSWER,
    PDF_LINES,
    REFUND_PHRASES,
    REFUSED_QUESTION,
    a_pdf,
)
from scripts.sla_scheduler_check import PASSWORD, Account, Evidence, admin, docker, request

ROOT = Path(__file__).resolve().parents[2]
DOMAIN = "live-ai.supportflow.example.com"
COMPOSE = ("compose", "--env-file", ".env.production", "-f", "docker-compose.prod.yml")
TEMP_ENV = ROOT / ".env.ai-checks.local"

# Provider messages may contain credentials or submitted text. Only known codes and our
# own explanations reach the console or report.
EMBEDDING_ERROR_REASONS = {
    "insufficient_quota": "Add OpenAI API credits before retrying live RAG verification",
    "credit_balance_exhausted": "Add OpenAI API credits before retrying live RAG verification",
    "organization_spend_limit_exceeded": "Review the OpenAI organization spend limit",
    "project_spend_limit_exceeded": "Review the OpenAI project spend limit",
    "organization_usage_limit_exceeded": "Review the OpenAI organization usage limit",
    "invalid_api_key": "The embedding provider rejected the configured API key",
    "model_not_found": "The configured embedding model is unavailable",
    "rate_limit_exceeded": "The embedding provider rate-limited the request; retry later",
    "slow_down": "The embedding provider rate-limited the request; retry later",
}

WITNESS = """
import json, sys, uuid
from datetime import datetime
from sqlalchemy import create_engine, text
from app.core.config import get_settings
p = json.load(sys.stdin)
engine = create_engine(get_settings().sqlalchemy_dsn)
with engine.connect() as connection:
    org = connection.execute(text('SELECT organization_id FROM users WHERE id = :id'),
                             {'id': uuid.UUID(p['owner_id'])}).scalar_one()
    params = {'org': org, 'since': datetime.fromisoformat(p['since'])}
    queries = {
        'usage': 'SELECT id, ticket_id, operation::text, provider, model, prompt_tokens, '
                 'completion_tokens, cost_usd, latency_ms, was_successful, was_cached, created_at '
                 'FROM ai_usage WHERE organization_id = :org AND created_at >= :since',
        'chunks': 'SELECT document_id, chunk_index, content, embedding_model, '
                  'vector_dims(embedding) AS dimensions FROM knowledge_chunks '
                  'WHERE organization_id = :org AND created_at >= :since',
    }
    result = {'organization_id': str(org)}
    for key, query in queries.items():
        result[key] = [dict(row) for row in connection.execute(text(query), params).mappings()]
print(json.dumps(result, default=str))
engine.dispose()
"""


def witness(owner: Account, since: str) -> dict[str, Any]:
    return cast(
        dict[str, Any],
        json.loads(
            docker(
                "exec",
                "-i",
                "supportflow-prod-backend-1",
                "python",
                "-c",
                WITNESS,
                input_text=json.dumps({"owner_id": owner.user["id"], "since": since}),
            )
        ),
    )


def credentials(generation_only: bool, *, embedding_only: bool = False) -> dict[str, str]:
    sources = [dotenv_values(ROOT / filename) for filename in (".env.production", ".env")]
    values: dict[str, str] = {}
    for prefix, default_provider, default_model in (
        ("AI", "anthropic", "claude-sonnet-5"),
        ("EMBEDDING", "openai", "text-embedding-3-small"),
    ):
        if embedding_only and prefix == "AI":
            continue
        source = next((row for row in sources if row.get(f"{prefix}_API_KEY")), None)
        if source is None:
            if prefix == "EMBEDDING" and generation_only:
                continue
            raise RuntimeError(f"Add {prefix}_API_KEY to a gitignored local environment file")
        values.update(
            {
                f"{prefix}_API_KEY": str(source[f"{prefix}_API_KEY"]),
                f"{prefix}_PROVIDER": source.get(f"{prefix}_PROVIDER") or default_provider,
                f"{prefix}_MODEL": source.get(f"{prefix}_MODEL") or default_model,
            }
        )
        if values[f"{prefix}_PROVIDER"] == "fake":
            raise RuntimeError("Live verification requires real providers")
    return values


def embedding_preflight(values: dict[str, str]) -> dict[str, Any]:
    """One short embedding request, with no retries, database writes, or raw responses.

    Match the application's request and vector width. Explicitly reducing dimensions here
    would conceal a model whose default output cannot fit the production pgvector column.
    This direct operator probe spends provider quota outside the application's usage ledger.
    """
    result: dict[str, Any] = {
        "checked_at": datetime.now(UTC).isoformat(),
        "provider": values["EMBEDDING_PROVIDER"],
        "model": values["EMBEDDING_MODEL"],
        "available": False,
        "http_status": None,
        "error_code": None,
        "embedding_dimensions": 0,
        "prompt_tokens": 0,
        "reason": "The embedding provider returned an invalid response",
    }
    if values["EMBEDDING_PROVIDER"] != "openai":
        result["reason"] = "Embedding preflight requires EMBEDDING_PROVIDER=openai"
        return result
    try:
        with httpx.Client(timeout=25) as client:
            response = client.post(
                "https://api.openai.com/v1/embeddings",
                headers={"Authorization": f"Bearer {values['EMBEDDING_API_KEY']}"},
                json={
                    "model": values["EMBEDDING_MODEL"],
                    "input": ["SupportFlow embedding access check"],
                },
            )
    except httpx.TimeoutException:
        result["reason"] = "The embedding provider timed out; retry later"
        return result
    except httpx.HTTPError:
        result["reason"] = "The embedding provider could not be reached; retry later"
        return result
    result["http_status"] = response.status_code
    try:
        payload = response.json()
    except ValueError:
        payload = {}
    if not response.is_success:
        error = payload.get("error") if isinstance(payload, dict) else None
        code = error.get("code") if isinstance(error, dict) else None
        if isinstance(code, str) and code in EMBEDDING_ERROR_REASONS:
            result["error_code"] = code
            result["reason"] = EMBEDDING_ERROR_REASONS[code]
        else:
            result["reason"] = f"The embedding provider returned HTTP {response.status_code}"
        return result
    if not isinstance(payload, dict):
        return result
    usage = payload.get("usage")
    tokens = usage.get("prompt_tokens") if isinstance(usage, dict) else None
    if isinstance(tokens, int) and not isinstance(tokens, bool) and tokens >= 0:
        result["prompt_tokens"] = tokens
    data = payload.get("data")
    if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
        return result
    if data[0].get("index") != 0:
        return result
    vector = data[0].get("embedding")
    if not isinstance(vector, list):
        return result
    result["embedding_dimensions"] = len(vector)
    try:
        embedding = Embedding.model_validate({"vectors": [vector]})
    except ValidationError:
        return result
    if not all(math.isfinite(value) for value in embedding.vectors[0]):
        return result
    result["available"] = True
    result["reason"] = (
        f"The embedding provider returned a valid {EMBEDDING_DIMENSIONS}-dimension vector"
    )
    return result


def wait_ready(context: ssl.SSLContext) -> None:
    deadline = time.monotonic() + 90
    with httpx.Client(verify=context, timeout=5) as client:
        while time.monotonic() < deadline:
            try:
                response = client.get("https://localhost/health/ready")
                if response.status_code == 200 and response.json().get("status") == "ready":
                    return
            except httpx.HTTPError:
                pass
            time.sleep(2)
    raise RuntimeError("Production HTTPS readiness did not recover")


def member(client: httpx.Client, owner: Account, customer: dict[str, Any]) -> Account:
    from scripts.mailbox_verification import verify_local_account

    email = f"customer@{DOMAIN}"
    users = request(client, "GET", "/users", owner)
    user = next((row for row in users if row["email"] == email), None)
    if user is None:
        user = request(
            client,
            "POST",
            "/users",
            owner,
            json={
                "name": "Live AI Customer",
                "email": email,
                "password": PASSWORD,
                "role": "customer",
                "customer_id": customer["id"],
            },
        )
    payload = {"email": email, "password": PASSWORD}
    if user.get("email_verified_at") is None:
        verified = verify_local_account(
            email,
            api_base="https://localhost/api/v1",
            client=client,
            mailpit_url="http://127.0.0.1:8026",
            user_id=user["id"],
        )
        payload["organization_slug"] = verified["organization_slug"]
    login = request(client, "POST", "/auth/login", json=payload)
    return Account(login["access_token"], user)


def run_checks(e: Evidence, client: httpx.Client, values: dict[str, str], only: bool) -> None:
    owner = admin(client, f"admin@{DOMAIN}", "Local live AI checks")
    foreign = admin(client, "admin@isolation.live-ai.example.com", "Local live AI isolation")
    customers = request(client, "GET", "/customers", owner)
    customer = next((row for row in customers if row["email"] == f"customer@{DOMAIN}"), None)
    if customer is None:
        customer = request(
            client,
            "POST",
            "/customers",
            owner,
            json={"name": "Live AI Customer", "email": f"customer@{DOMAIN}"},
        )
    portal = member(client, owner, customer)
    since = datetime.now(UTC).isoformat()
    e.details["provider"] = {k: v for k, v in values.items() if not k.endswith("API_KEY")}
    e.details["scope"] = "generation" if only else "generation_and_rag"
    tickets: list[dict[str, Any]] = []
    documents: list[dict[str, Any]] = []

    def call(method: str, path: str, who: Account = owner, **kwargs: Any) -> Any:
        return request(client, method, path, who, **kwargs)

    def usage() -> dict[str, Any]:
        return cast(dict[str, Any], call("GET", "/analytics/overview")["ai_usage"])

    def settled(
        ticket: dict[str, Any], operations: set[str], ids: set[str] | None = None
    ) -> list[dict[str, Any]]:
        deadline = time.monotonic() + 150
        rows: list[dict[str, Any]] = []
        while time.monotonic() < deadline:
            rows = [
                row
                for row in call("GET", f"/tickets/{ticket['id']}/ai/analyses")
                if row["operation"] in operations and (ids is None or row["id"] in ids)
            ]
            if len(rows) == len(operations) and all(
                row["status"] in ("completed", "failed") for row in rows
            ):
                break
            time.sleep(2)
        e.details.setdefault("analyses", []).extend(rows)
        e.require(
            f"{', '.join(sorted(operations))} completed via the live worker",
            len(rows) == len(operations) and all(row["status"] == "completed" for row in rows),
        )
        return rows

    def ticket(subject: str, description: str) -> dict[str, Any]:
        row = call(
            "POST",
            "/tickets",
            json={
                "subject": subject,
                "description": description,
                "priority": "low",
                "customer_id": customer["id"],
            },
        )
        tickets.append(row)
        e.details["tickets"] = tickets
        return cast(dict[str, Any], row)

    try:
        before = usage()
        row = ticket(
            f"Live AI duplicate charge {uuid.uuid4().hex[:8]}",
            "I was charged twice for order 88213. Three emails went unanswered. "
            "I am going to dispute the charge with my bank.",
        )
        path = f"/tickets/{row['id']}"
        initial = settled(row, {"classify", "sentiment"})
        e.check(
            "analysis rows name the configured real generation model",
            all(
                item["provider"] == values["AI_PROVIDER"] and item["model"] == values["AI_MODEL"]
                for item in initial
            ),
        )
        current = call("GET", path)
        classification = next(item["result"] for item in initial if item["operation"] == "classify")
        sentiment = next(item["result"] for item in initial if item["operation"] == "sentiment")
        e.check(
            "classification is mapped onto the ticket without changing its priority",
            current["priority"] == "low"
            and classification
            == {
                "category": current["category"],
                "subcategory": current["subcategory"],
                "priority": current["ai_recommended_priority"],
                "confidence": current["ai_classification_confidence"],
            },
        )
        e.check(
            "sentiment matches the validated persisted result",
            sentiment
            == {
                "sentiment": current["sentiment"],
                "confidence": current["sentiment_confidence"],
            },
        )
        e.check(
            "both live analysis calls report tokens and latency",
            all(
                item["prompt_tokens"] > 0
                and item["completion_tokens"] > 0
                and item["latency_ms"] >= 0
                for item in initial
            ),
        )
        call(
            "POST",
            f"{path}/messages",
            portal,
            json={"body": "Please confirm when you can fix this."},
        )
        call(
            "POST",
            f"{path}/notes",
            json={"body": "Investigating duplicate capture. No refund approved yet."},
        )
        snapshot = call("GET", path)
        queued = call("POST", f"{path}/ai/summarize")
        summary = settled(row, {"summarize"}, {queued["id"]})[0]
        after_summary = call("GET", path)
        stable_fields = (
            "category",
            "subcategory",
            "priority",
            "ai_recommended_priority",
            "ai_classification_confidence",
            "sentiment",
            "sentiment_confidence",
            "status",
            "first_response_at",
            "resolved_at",
        )
        e.check(
            "summary contains prose and leaves ticket classification and lifecycle unchanged",
            len(summary["result"]["summary"]) > 40
            and all(after_summary[key] == snapshot[key] for key in stable_fields),
        )
        baseline = usage()
        cached = call("POST", f"{path}/ai/summarize")
        after = usage()
        e.check(
            "unchanged conversation reuses the same completed summary",
            cached["id"] == summary["id"] and cached["status"] == "completed",
        )
        e.check(
            "summary cache hit adds one cached call and no cost",
            after["calls"] == baseline["calls"] + 1
            and after["cached_calls"] == baseline["cached_calls"] + 1
            and after["cost_usd"] == baseline["cost_usd"],
        )
        call(
            "POST",
            f"{path}/messages",
            portal,
            json={"body": "The duplicate charge is now posted on my bank statement."},
        )
        fresh = call("POST", f"{path}/ai/summarize")
        e.check("a new message invalidates the summary cache", fresh["id"] != summary["id"])
        settled(row, {"summarize"}, {fresh["id"]})
        public_before = call("GET", f"{path}/messages", portal)
        queued = call("POST", f"{path}/ai/suggest-response")
        settled(row, {"suggest_response"}, {queued["id"]})
        thread = call("GET", f"{path}/messages")
        drafts = [item for item in thread if item["sender_type"] == "ai_draft"]
        e.require(
            "one internal AI draft was written", len(drafts) == 1 and drafts[0]["is_internal"]
        )
        e.check(
            "AI draft and internal note remain invisible to the customer",
            call("GET", f"{path}/messages", portal) == public_before,
        )
        regenerated = call("POST", f"{path}/ai/suggest-response")
        e.check("draft regeneration creates a fresh analysis", regenerated["id"] != queued["id"])
        settled(row, {"suggest_response"}, {regenerated["id"]})
        drafts = [
            item for item in call("GET", f"{path}/messages") if item["sender_type"] == "ai_draft"
        ]
        e.check(
            "regeneration keeps both draft versions internal",
            len(drafts) == 2 and all(item["is_internal"] for item in drafts),
        )
        baseline = usage()
        sent_body = (
            "Thanks for reporting this. We are investigating the duplicate charge "
            "and will update you."
        )
        sent = call("POST", f"{path}/ai/drafts/{drafts[-1]['id']}/accept", json={"body": sent_body})
        after = usage()
        e.check(
            "human acceptance posts the edited public reply without another model call",
            sent["body"] == sent_body
            and not sent["is_internal"]
            and after["calls"] == baseline["calls"]
            and after["cost_usd"] == baseline["cost_usd"],
        )
        portal_thread = call("GET", f"{path}/messages", portal)
        e.check(
            "customer sees only the accepted reply and public conversation",
            any(item["id"] == sent["id"] for item in portal_thread)
            and all(
                not item["is_internal"] and item["sender_type"] != "ai_draft"
                for item in portal_thread
            ),
        )
        audit = call("GET", "/audit-logs", params={"target_type": "ticket", "target_id": row["id"]})
        e.check(
            "regeneration and edited acceptance are audited",
            any(item["action"] == "ai_response_regenerated" for item in audit)
            and any(
                item["action"] == "ai_response_accepted" and item["extra_data"].get("edited")
                for item in audit
            ),
        )
        for route in ("ai/analyses", "ai/summarize", "ai/suggest-response"):
            response = client.request(
                "GET" if route.endswith("analyses") else "POST",
                f"/api/v1{path}/{route}",
                headers=foreign.headers,
            )
            e.check(f"another tenant cannot access {route}", response.status_code == 404)
        after = usage()
        e.check(
            "dashboard reports live token usage and list-price cost",
            after["calls"] > before["calls"]
            and after["prompt_tokens"] > before["prompt_tokens"]
            and Decimal(after["cost_usd"]) > Decimal(before["cost_usd"]),
        )
        if only:
            e.details["rag_pending"] = "RAG checks were not run in --generation-only mode"
        else:
            rag_checks(
                e, client, owner, foreign, portal, customer, values, tickets, documents, since
            )
        stored = witness(owner, since)
        e.details["database"] = stored
        calls = stored["usage"]
        e.check(
            "read-only database witness confirms successful real-provider usage",
            bool(calls)
            and all(item["was_successful"] for item in calls)
            and all(item["provider"] != "fake" for item in calls),
        )
        e.check(
            "persisted cache-hit rows have zero tokens and cost",
            any(item["was_cached"] for item in calls)
            and all(
                item["prompt_tokens"] == item["completion_tokens"] == 0
                and Decimal(item["cost_usd"]) == 0
                for item in calls
                if item["was_cached"]
            ),
        )
    finally:
        # Preserve scoped evidence before cleanup and container recreation on failures too.
        if "database" not in e.details:
            try:
                e.details["database"] = witness(owner, since)
            except Exception:
                e.check("database evidence captured before cleanup", False)
        for document in documents:
            try:
                response = client.delete(
                    f"/api/v1/knowledge/{document['id']}", headers=owner.headers
                )
                e.check(f"test document {document['title']} removed", response.status_code == 204)
            except httpx.HTTPError:
                e.check("test document cleanup completed", False)
        for row in tickets:
            try:
                path = f"/tickets/{row['id']}"
                # A staff assignee is required before entering the active lifecycle.
                users = call("GET", "/users")
                agent = next((user for user in users if user["role"] == "agent"), None)
                if agent is None:
                    agent = call(
                        "POST",
                        "/users",
                        json={
                            "name": "Live AI Agent",
                            "role": "agent",
                            "email": f"agent@{DOMAIN}",
                            "password": PASSWORD,
                        },
                    )
                call("POST", f"{path}/assign", json={"assigned_agent_id": agent["id"]})
                call("POST", f"{path}/status", json={"status": "in_progress"})
                call("POST", f"{path}/status", json={"status": "resolved"})
                call("POST", f"{path}/close")
                e.check(
                    f"test ticket #{row['number']} closed", call("GET", path)["status"] == "closed"
                )
            except Exception:
                e.check(f"test ticket #{row['number']} cleanup completed", False)


def rag_checks(
    e: Evidence,
    client: httpx.Client,
    owner: Account,
    foreign: Account,
    portal: Account,
    customer: dict[str, Any],
    values: dict[str, str],
    tickets: list[dict[str, Any]],
    documents: list[dict[str, Any]],
    since: str,
) -> None:
    def call(method: str, path: str, **kwargs: Any) -> Any:
        return request(client, method, path, owner, **kwargs)

    for filename, title, data, media in (
        ("shipping.md", "Live shipping policy", MARKDOWN.encode(), "text/markdown"),
        ("refunds.pdf", "Live returns and refunds", a_pdf(PDF_LINES), "application/pdf"),
    ):
        documents.append(
            call(
                "POST",
                "/knowledge/upload",
                files={"file": (filename, data, media)},
                data={"title": title},
            )
        )
    documents.append(
        call(
            "POST",
            "/knowledge",
            json={
                "title": "Live support hours",
                "content": "Support operates 08:00 to 18:00 UTC, Monday to Friday.",
            },
        )
    )
    e.details["documents"] = documents
    for document in documents:
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            state = call("GET", f"/knowledge/{document['id']}")
            e.details.setdefault("document_states", {})[document["id"]] = state
            if state["status"] in ("completed", "failed"):
                break
            time.sleep(2)
        e.require(
            f"{document['title']} ingested, published and chunked by the live worker",
            state["status"] == "completed" and state["is_published"] and state["chunk_count"] > 0,
        )
    stored = witness(owner, since)
    e.check(
        "stored vectors have 1536 dimensions and the configured embedding model",
        len(stored["chunks"]) >= 3
        and all(
            item["dimensions"] == 1536 and item["embedding_model"] == values["EMBEDDING_MODEL"]
            for item in stored["chunks"]
        ),
    )
    answered = call("POST", "/knowledge/search", json={"question": ANSWERED_QUESTION})
    e.details["answer"] = answered
    e.require(
        "refund question has a grounded answer with source references",
        bool(answered["sources"])
        and any(phrase in answered["answer"].lower() for phrase in REFUND_PHRASES),
    )
    e.check(
        "each citation resolves to an exact stored tenant chunk",
        all(
            any(
                source["document_id"] == chunk["document_id"]
                and source["chunk_index"] == chunk["chunk_index"]
                and source["excerpt"] == chunk["content"]
                for chunk in stored["chunks"]
            )
            for source in answered["sources"]
        ),
    )
    e.check(
        "refund policy is the highest-ranked cited document",
        answered["sources"][0]["document_id"] == documents[1]["id"]
        and any(phrase in answered["sources"][0]["excerpt"].lower() for phrase in REFUND_PHRASES),
    )
    before = witness(owner, since)
    refused = call("POST", "/knowledge/search", json={"question": REFUSED_QUESTION})
    after = witness(owner, since)
    e.check(
        "unsupported question is refused with no sources",
        refused["answer"] == NO_ANSWER and refused["sources"] == [],
    )

    def count(rows: list[dict[str, Any]]) -> int:
        return sum(item["operation"] == "knowledge_answer" for item in rows)

    e.check(
        "grounded answer has a successful real generation ledger entry",
        count(before["usage"]) >= 1
        and all(
            item["was_successful"] and item["provider"] == values["AI_PROVIDER"]
            for item in before["usage"]
            if item["operation"] == "knowledge_answer"
        ),
    )

    e.check("refusal makes no generation call", count(before["usage"]) == count(after["usage"]))
    response = client.get(f"/api/v1/knowledge/{documents[0]['id']}", headers=foreign.headers)
    e.check("another tenant cannot read the ingested documents", response.status_code == 404)
    response = request(
        client, "POST", "/knowledge/search", foreign, json={"question": ANSWERED_QUESTION}
    )
    e.check(
        "another tenant's empty knowledge base cannot retrieve our policy",
        response["answer"] == NO_ANSWER and response["sources"] == [],
    )
    row = call(
        "POST",
        "/tickets",
        json={
            "customer_id": customer["id"],
            "priority": "low",
            "subject": "When will my refund arrive?",
            "description": "My return was approved. How long will the refund take?",
        },
    )
    tickets.append(row)
    deadline = time.monotonic() + 150
    while time.monotonic() < deadline:
        analyses = call("GET", f"/tickets/{row['id']}/ai/analyses")
        if len(analyses) == 2 and all(
            item["status"] in ("completed", "failed") for item in analyses
        ):
            break
        time.sleep(2)
    queued = call("POST", f"/tickets/{row['id']}/ai/suggest-response")
    deadline = time.monotonic() + 150
    result: dict[str, Any] = {}
    while time.monotonic() < deadline:
        result = next(
            (
                item
                for item in call("GET", f"/tickets/{row['id']}/ai/analyses")
                if item["id"] == queued["id"]
            ),
            {},
        )
        if result.get("status") in ("completed", "failed"):
            break
        time.sleep(2)
    e.require(
        "RAG-grounded draft completes and includes the documented refund timing",
        result.get("status") == "completed"
        and any(phrase in result["result"]["body"].lower() for phrase in REFUND_PHRASES),
    )
    stored = witness(owner, since)
    e.check(
        "grounded drafting attributes an embedding call to its ticket",
        any(
            item["operation"] == "embed"
            and item["ticket_id"] == row["id"]
            and item["was_successful"]
            for item in stored["usage"]
        ),
    )
    thread = request(client, "GET", f"/tickets/{row['id']}/messages", portal)
    e.check(
        "grounded draft is not automatically sent to the customer",
        all(item["sender_type"] != "ai_draft" for item in thread),
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--generation-only", action="store_true", help="Skip embedding preflight and RAG"
    )
    mode.add_argument(
        "--preflight-only",
        action="store_true",
        help="Check embedding access without starting Docker",
    )
    args = parser.parse_args()
    e = Evidence()
    e.details["started_at"] = datetime.now(UTC).isoformat()
    report = (
        ROOT
        / "backend"
        / (
            ".live-ai-generation-report.local"
            if args.generation_only
            else ".embedding-preflight-report.local"
        )
    )
    prepared = False
    try:
        values = credentials(args.generation_only, embedding_only=args.preflight_only)
        if not args.preflight_only and TEMP_ENV.exists():
            raise RuntimeError("Temporary AI environment file already exists; inspect before reuse")
        if not args.generation_only:
            print("Checking embedding access with one short provider request", flush=True)
            preflight = embedding_preflight(values)
            e.details["scope"] = "embedding_preflight"
            e.details["embedding_preflight"] = preflight
            e.check("embedding provider access and vector width verified", preflight["available"])
            print(preflight["reason"], flush=True)
            if not preflight["available"]:
                return 1
            if args.preflight_only:
                return 0
            # A prerequisite failure must not overwrite the earlier full-run evidence.
            report = ROOT / "backend" / ".live-ai-rag-report.local"
        # A stopped daemon must fail before a secret override file needs restoration.
        docker("info", "--format", "{{.ServerVersion}}")
        # Complete settings from the existing production file, with only AI fields replaced.
        with TEMP_ENV.open("x", encoding="utf-8") as stream:
            stream.write((ROOT / ".env.production").read_text(encoding="utf-8"))
        prepared = True
        for key, value in values.items():
            set_key(str(TEMP_ENV), key, value)
        print(
            "Starting local production stack with temporary live-provider credentials", flush=True
        )
        docker(*COMPOSE, "-f", "docker-compose.ai-checks.yml", "up", "-d")
        docker(
            *COMPOSE,
            "cp",
            "caddy:/data/caddy/pki/authorities/local/root.crt",
            "backend/.caddy-local-root.pem",
        )
        context = ssl.create_default_context(cafile=str(ROOT / "backend" / ".caddy-local-root.pem"))
        wait_ready(context)
        runtime = json.loads(
            docker(
                "exec",
                "supportflow-prod-worker-1",
                "python",
                "-c",
                "import json; from app.core.config import get_settings; s=get_settings(); "
                "print(json.dumps({'provider':s.AI_PROVIDER,'model':s.AI_MODEL,'has_key':bool(s.AI_API_KEY)}))",
            )
        )
        e.require(
            "worker uses the configured real provider with a credential",
            runtime
            == {"provider": values["AI_PROVIDER"], "model": values["AI_MODEL"], "has_key": True},
        )
        with httpx.Client(base_url="https://localhost", verify=context, timeout=65) as client:
            run_checks(e, client, values, args.generation_only)
    except Exception as exc:
        e.check(f"live verification completed ({type(exc).__name__})", False)
        if isinstance(exc, RuntimeError):
            print(str(exc), flush=True)
    finally:
        if prepared:
            try:
                print("Restoring normal production provider configuration", flush=True)
                docker(*COMPOSE, "up", "-d", "--remove-orphans")
                desired = json.loads(docker(*COMPOSE, "config", "--format", "json"))["services"]
                for service in ("backend", "worker"):
                    container = json.loads(docker("inspect", f"supportflow-prod-{service}-1"))[0]
                    actual = dict(item.split("=", 1) for item in container["Config"]["Env"])
                    keys = {key for key in actual if key.startswith(("AI_", "EMBEDDING_"))}
                    e.check(
                        f"{service} original AI environment restored",
                        all(
                            actual[key] == str(desired[service]["environment"].get(key, ""))
                            for key in keys
                        ),
                    )
                TEMP_ENV.unlink()
                e.check("temporary credential file removed", not TEMP_ENV.exists())
            except Exception:
                e.check("production configuration restoration completed", False)
        e.details.update(
            {
                "finished_at": datetime.now(UTC).isoformat(),
                "checks": e.checks,
                "passed": sum(item["passed"] for item in e.checks),
                "failed": sum(not item["passed"] for item in e.checks),
            }
        )
        report.write_text(json.dumps(e.details, indent=2), encoding="utf-8")
        print(
            f"{e.details['passed']} passed, {e.details['failed']} failed. Report: {report}",
            flush=True,
        )
    return 1 if e.details["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
