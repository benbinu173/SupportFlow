"""Observe real, scheduler-driven SLA alerts on the local production stack.

Run from backend with the local mail-capture and five-second scheduler overrides:
    .venv/Scripts/python.exe -m scripts.sla_scheduler_check

All application traffic uses verified HTTPS/WSS through Caddy. Policies are changed
only in the dedicated test tenant. Tickets age naturally; no timestamp is rewritten
and no Celery task is invoked or published by this script. Docker access is used for
read-only database witnesses, runtime settings, and scheduler/worker logs.
Policies are restored and newly created tickets closed in finally. The caller must
restore the regular compose stack afterwards to remove the local testing overrides.
"""

# Operator tooling reports to a terminal, as the other walkthrough scripts do.
# ruff: noqa: T201

import json
import shutil
import ssl
import subprocess
import sys
import threading
import time
import uuid
from contextlib import ExitStack
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from socket import create_connection
from typing import Any, cast

import httpx
from websockets.sync.client import connect

ROOT = Path(__file__).resolve().parents[2]
BASE = "https://localhost"
MAILPIT = "http://127.0.0.1:8026"
DOMAIN = "supportflow-sla.example.com"
PASSWORD = "LocalSlaPassw0rd!23"
KINDS = {"sla_warning", "sla_breached"}
EXPECTED = {(timer, kind) for timer in ("response", "resolution") for kind in KINDS}
DOCKER = shutil.which("docker")

DATABASE_WITNESS = """
import json, sys, uuid
from sqlalchemy import bindparam, create_engine, text
from app.core.config import get_settings

payload = json.load(sys.stdin)
engine = create_engine(str(get_settings().DATABASE_URL))
with engine.connect() as connection:
    organization_id = connection.execute(
        text('SELECT organization_id FROM users WHERE id = :id AND email = :email'),
        {'id': uuid.UUID(payload['admin_id']), 'email': payload['admin_email']},
    ).scalar_one()
    params = {'org': organization_id, 'ids': [uuid.UUID(value) for value in payload['ids']]}
    queries = {
        'events': 'SELECT id, ticket_id, event_type::text, actor_user_id, extra_data, created_at '
                  'FROM ticket_events WHERE organization_id = :org AND ticket_id IN :ids '
                  "AND event_type::text IN ('sla_warning', 'sla_breached') ORDER BY created_at",
        'notifications': 'SELECT id, ticket_id, user_id, notification_type::text, title, '
                         'body, emailed_at, created_at FROM notifications '
                         'WHERE organization_id = :org AND ticket_id IN :ids '
                         "AND notification_type::text IN ('sla_warning', 'sla_breached')",
    }
    result = {'organization_id': str(organization_id)}
    for key, query in queries.items():
        rows = connection.execute(text(query).bindparams(bindparam('ids', expanding=True)), params)
        result[key] = [dict(row) for row in rows.mappings()]
print(json.dumps(result, default=str))
engine.dispose()
"""


@dataclass
class Evidence:
    checks: list[dict[str, Any]] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)

    def check(self, label: str, condition: bool) -> None:
        self.checks.append({"label": label, "passed": condition})
        print(f"  {'ok' if condition else 'FAIL':<4} {label}", flush=True)

    def require(self, label: str, condition: bool) -> None:
        self.check(label, condition)
        if not condition:
            raise RuntimeError(label)


@dataclass
class Account:
    token: str
    user: dict[str, Any]

    @property
    def headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}


def docker(*args: str, input_text: str | None = None) -> str:
    if DOCKER is None:
        raise RuntimeError("Docker CLI is unavailable")
    result = subprocess.run(  # noqa: S603 -- fixed operator commands, no shell or credentials.
        [DOCKER, *args],
        input=input_text,
        text=True,
        capture_output=True,
        timeout=30,
        cwd=ROOT,
    )
    if result.returncode:
        # Command errors may carry environment values; report the exit code only.
        raise RuntimeError(f"Docker command failed with exit code {result.returncode}")
    return result.stdout


def request(
    client: httpx.Client, method: str, path: str, account: Account | None = None, **kwargs: Any
) -> Any:
    response = client.request(
        method, f"/api/v1{path}", headers=account.headers if account else {}, **kwargs
    )
    if not response.is_success:
        raise RuntimeError(f"{method} {path}: HTTP {response.status_code}")
    return response.json()


def admin(client: httpx.Client, email: str, organization: str) -> Account:
    response = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    if response.status_code == 401:
        response = client.post(
            "/api/v1/auth/register",
            json={
                "organization_name": organization,
                "name": "SLA Check Admin",
                "email": email,
                "password": PASSWORD,
            },
        )
    if not response.is_success:
        raise RuntimeError(f"Test tenant login/registration: HTTP {response.status_code}")
    token = response.json()["access_token"]
    account = Account(token, {})
    account.user = request(client, "GET", "/auth/me", account)
    return account


def member(client: httpx.Client, owner: Account, role: str, name: str) -> Account:
    email = f"{name}@{DOMAIN}"
    users = request(client, "GET", "/users", owner)
    user = next((user for user in users if user["email"] == email), None)
    if user is None:
        user = request(
            client,
            "POST",
            "/users",
            owner,
            json={
                "name": name.title(),
                "email": email,
                "password": PASSWORD,
                "role": role,
            },
        )
    token = request(client, "POST", "/auth/login", json={"email": email, "password": PASSWORD})
    return Account(token["access_token"], user)


def stamp(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def events(client: httpx.Client, owner: Account, ticket: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        event
        for event in request(client, "GET", f"/tickets/{ticket['id']}/events", owner)
        if event["event_type"] in KINDS
    ]


def snapshot(owner: Account, tickets: list[dict[str, Any]]) -> dict[str, Any]:
    payload = {
        "admin_id": owner.user["id"],
        "admin_email": owner.user["email"],
        "ids": [ticket["id"] for ticket in tickets],
    }
    return cast(
        dict[str, Any],
        json.loads(
            docker(
                "exec",
                "-i",
                "supportflow-prod-backend-1",
                "python",
                "-c",
                DATABASE_WITNESS,
                input_text=json.dumps(payload),
            )
        ),
    )


def logs(service: str, since: datetime) -> list[dict[str, Any]]:
    # Docker writes container stderr to its own stderr. Merge it into stdout inside this
    # subprocess result, without printing unrelated tenant records or broker settings.
    if DOCKER is None:
        raise RuntimeError("Docker CLI is unavailable")
    result = subprocess.run(  # noqa: S603 -- fixed local container and timestamp, no shell.
        [DOCKER, "logs", "--since", since.isoformat(), f"supportflow-prod-{service}-1"],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode:
        raise RuntimeError("Cannot read scheduler/worker evidence")
    records = []
    for line in (result.stdout + result.stderr).splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            # Celery wraps structlog's JSON message in its own JSON log envelope.
            message = record.get("event")
            if isinstance(message, str) and message.startswith("{"):
                try:
                    inner = json.loads(message)
                except ValueError:
                    pass
                else:
                    if isinstance(inner, dict):
                        record = inner
            records.append(record)
    return records


def mailbox(client: httpx.Client, tickets: list[dict[str, Any]]) -> list[dict[str, Any]]:
    response = client.get(f"{MAILPIT}/api/v1/messages", params={"limit": 500})
    response.raise_for_status()
    matches = []
    for mail in response.json()["messages"]:
        if not mail["Subject"].startswith(
            ("[SupportFlow] SLA warning:", "[SupportFlow] SLA breach:")
        ):
            continue
        response = client.get(f"{MAILPIT}/api/v1/message/{mail['ID']}")
        response.raise_for_status()
        body = response.json()["Text"]
        ticket = next(
            (ticket for ticket in tickets if f"#{ticket['number']} - {ticket['subject']}" in body),
            None,
        )
        if ticket:
            matches.append(
                {
                    "id": mail["ID"],
                    "ticket_id": ticket["id"],
                    "subject": mail["Subject"],
                    "to": [recipient["Address"] for recipient in mail["To"]],
                }
            )
    return matches


def observe(
    socket: Any, frames: list[dict[str, Any]], errors: list[str], stop: threading.Event
) -> None:
    while not stop.is_set():
        try:
            frame = json.loads(socket.recv(timeout=1))
        except TimeoutError:
            continue
        except Exception as exc:
            if not stop.is_set():
                errors.append(type(exc).__name__)
            return
        if frame.get("type") == "notification.created" and frame.get("notification_type") in KINDS:
            frames.append(frame)


def run(evidence: Evidence, client: httpx.Client, context: ssl.SSLContext) -> None:
    started = datetime.now(UTC)
    evidence.details["started_at"] = started.isoformat()
    ready = client.get("/health/ready")
    evidence.require(
        "production dependencies are ready through verified HTTPS",
        ready.status_code == 200 and ready.json().get("status") == "ready",
    )
    runtime = json.loads(
        docker(
            "exec",
            "supportflow-prod-worker-1",
            "python",
            "-c",
            "import json; from app.core.config import get_settings; s=get_settings(); "
            "print(json.dumps({'smtp':s.SMTP_HOST,'port':s.SMTP_PORT,'tls':s.SMTP_STARTTLS}))",
        )
    )
    evidence.require(
        "worker mail is captured locally",
        runtime == {"smtp": "mailpit", "port": 1025, "tls": False},
    )
    interval = docker(
        "exec",
        "supportflow-prod-beat-1",
        "python",
        "-c",
        "from app.core.config import get_settings; "
        "print(get_settings().SLA_SWEEP_INTERVAL_SECONDS)",
    )
    evidence.require("temporary scheduler interval is five seconds", interval.strip() == "5")
    owner = admin(client, f"admin@{DOMAIN}", "Local SLA Scheduler Checks")
    foreign = admin(client, "admin@sla-isolation.example.com", "Local SLA Isolation Checks")
    accounts = {
        "admin": owner,
        "agent": member(client, owner, "agent", "agent"),
        "manager1": member(client, owner, "manager", "manager1"),
        "manager2": member(client, owner, "manager", "manager2"),
        "foreign": foreign,
    }
    customers = request(client, "GET", "/customers", owner)
    customer = next((row for row in customers if row["email"] == f"customer@{DOMAIN}"), None)
    if customer is None:
        customer = request(
            client,
            "POST",
            "/customers",
            owner,
            json={"name": "SLA Test Customer", "email": f"customer@{DOMAIN}"},
        )
    policies = request(client, "GET", "/sla/policies", owner)
    tickets: list[dict[str, Any]] = []
    suffix = uuid.uuid4().hex[:8]

    def ticket(label: str, priority: str = "urgent", assigned: bool = False) -> dict[str, Any]:
        row = request(
            client,
            "POST",
            "/tickets",
            owner,
            json={
                "customer_id": customer["id"],
                "subject": f"SLA scheduler {label} {suffix}",
                "description": "Local scheduler verification.",
                "priority": priority,
            },
        )
        tickets.append(row)
        evidence.details["tickets"] = [
            {key: item[key] for key in ("id", "number", "subject", "created_at")}
            for item in tickets
        ]
        if assigned:
            request(
                client,
                "POST",
                f"/tickets/{row['id']}/assign",
                owner,
                json={"assigned_agent_id": accounts["agent"].user["id"]},
            )
        return cast(dict[str, Any], row)

    try:
        for priority in ("urgent", "low"):
            request(
                client,
                "PATCH",
                f"/sla/policies/{priority}",
                owner,
                json={
                    "response_time_minutes": 1,
                    "resolution_time_minutes": 2,
                    "warning_threshold_percent": 50,
                    "is_active": priority == "urgent",
                },
            )
        met = ticket("met-control", assigned=True)
        request(
            client, "POST", f"/tickets/{met['id']}/status", owner, json={"status": "in_progress"}
        )
        request(
            client,
            "POST",
            f"/tickets/{met['id']}/messages",
            accounts["agent"],
            json={"body": "Answered on time."},
        )
        request(client, "POST", f"/tickets/{met['id']}/status", owner, json={"status": "resolved"})
        inactive = ticket("inactive-control", priority="low")
        stop = threading.Event()
        socket_errors: list[str] = []
        received: dict[str, list[dict[str, Any]]] = {role: [] for role in accounts}
        threads: list[threading.Thread] = []
        with ExitStack() as stack:
            for role, account in accounts.items():
                print(f"  connecting {role} WSS observer", flush=True)
                # Use Docker Desktop's IPv4 loopback while retaining localhost for TLS
                # certificate and hostname verification. No external proxy is involved.
                transport = stack.enter_context(create_connection(("127.0.0.1", 443), timeout=15))
                socket = stack.enter_context(
                    connect(
                        "wss://localhost/ws",
                        sock=transport,
                        ssl=context,
                        proxy=None,
                        open_timeout=15,
                    )
                )
                print(f"  {role} WSS upgrade completed", flush=True)
                socket.send(json.dumps({"type": "auth", "token": account.token}))
                evidence.require(
                    f"{role} WSS session authenticates",
                    json.loads(socket.recv(timeout=15)).get("type") == "authenticated",
                )
                thread = threading.Thread(
                    target=observe, args=(socket, received[role], socket_errors, stop), daemon=True
                )
                thread.start()
                threads.append(thread)
            try:
                assigned = ticket("assigned", assigned=True)
                request(
                    client,
                    "POST",
                    f"/tickets/{assigned['id']}/notes",
                    accounts["agent"],
                    json={"body": "An internal note must not stop the response timer."},
                )
                unassigned = ticket("unassigned")
                current = request(client, "GET", f"/tickets/{assigned['id']}", owner)
                evidence.require(
                    "internal note leaves the response clock running",
                    current["first_response_at"] is None,
                )
                evidence.require("all four test scenarios were created", len(tickets) == 4)
                observed: dict[str, set[tuple[str, str]]] = {
                    assigned["id"]: set(),
                    unassigned["id"]: set(),
                }
                deadline = time.monotonic() + 160
                while time.monotonic() < deadline:
                    for row in (assigned, unassigned):
                        for event in events(client, owner, row):
                            key = (event["extra_data"]["timer"], event["event_type"])
                            if key not in observed[row["id"]]:
                                observed[row["id"]].add(key)
                                elapsed = (
                                    stamp(event["created_at"]) - stamp(row["created_at"])
                                ).total_seconds()
                                print(
                                    f"  observed #{row['number']} {key[0]} {key[1]} "
                                    f"at {elapsed:.1f}s",
                                    flush=True,
                                )
                    if all(keys == EXPECTED for keys in observed.values()):
                        break
                    time.sleep(1)
                for row in (assigned, unassigned):
                    evidence.require(
                        f"#{row['number']} received warning and breach for both timers",
                        observed[row["id"]] == EXPECTED,
                    )
                stored = snapshot(owner, tickets)
                organization_id = stored["organization_id"]
                for row in (assigned, unassigned):
                    rows = [event for event in stored["events"] if event["ticket_id"] == row["id"]]
                    evidence.check(
                        f"#{row['number']} has exactly four system-authored timeline alerts",
                        len(rows) == 4 and all(event["actor_user_id"] is None for event in rows),
                    )
                    delays = []
                    for event in rows:
                        timer = event["extra_data"]["timer"]
                        target = 60 if timer == "response" else 120
                        due = stamp(row["created_at"]) + timedelta(seconds=target)
                        instant = (
                            due
                            if event["event_type"] == "sla_breached"
                            else stamp(row["created_at"]) + timedelta(seconds=target / 2)
                        )
                        delay = (stamp(event["created_at"]) - instant).total_seconds()
                        delays.append(
                            {
                                "timer": timer,
                                "kind": event["event_type"],
                                "delay_seconds": round(delay, 3),
                            }
                        )
                        evidence.check(
                            f"#{row['number']} {timer} {event['event_type']}: "
                            "correct deadline and threshold timing",
                            stamp(event["extra_data"]["due_at"]) == due and 0 <= delay <= 30,
                        )
                    evidence.details.setdefault("alert_delays", {})[row["id"]] = delays
                expected_recipients = {
                    assigned["id"]: {
                        accounts[role].user["id"] for role in ("agent", "manager1", "manager2")
                    },
                    unassigned["id"]: {
                        accounts[role].user["id"] for role in ("manager1", "manager2")
                    },
                }
                for row in (assigned, unassigned):
                    notifications = [
                        item for item in stored["notifications"] if item["ticket_id"] == row["id"]
                    ]
                    evidence.check(
                        f"#{row['number']} has the exact expected notification fan-out",
                        len(notifications) == 4 * len(expected_recipients[row["id"]])
                        and all(
                            sum(item["user_id"] == recipient for item in notifications) == 4
                            for recipient in expected_recipients[row["id"]]
                        ),
                    )
                relevant_ids = {assigned["id"], unassigned["id"]}
                expected_ids = {item["id"] for item in stored["notifications"]}
                mail_deadline = time.monotonic() + 30
                mails: list[dict[str, Any]] = []
                while time.monotonic() < mail_deadline:
                    stored = snapshot(owner, tickets)
                    mails = mailbox(client, tickets)
                    actual_ids = {
                        frame["notification_id"]
                        for frames in received.values()
                        for frame in frames
                        if frame.get("ticket_id") in relevant_ids
                    }
                    if (
                        len(mails) == 20
                        and actual_ids == expected_ids
                        and all(item["emailed_at"] for item in stored["notifications"])
                    ):
                        break
                    time.sleep(1)
                for role, account in accounts.items():
                    frames = [
                        frame for frame in received[role] if frame.get("ticket_id") in relevant_ids
                    ]
                    expected_for_user = {
                        item["id"]
                        for item in stored["notifications"]
                        if item["user_id"] == account.user["id"]
                    }
                    evidence.check(
                        f"{role} receives exactly its own scheduled alerts over WSS",
                        len(frames) == len(expected_for_user)
                        and {frame["notification_id"] for frame in frames} == expected_for_user
                        and all(
                            frame["user_id"] == account.user["id"]
                            and frame["organization_id"] == organization_id
                            for frame in frames
                        ),
                    )
                    inbox = request(client, "GET", "/notifications", account, params={"limit": 100})
                    own = {
                        item["id"]
                        for item in inbox
                        if item.get("ticket_id") in relevant_ids
                        and item["notification_type"] in KINDS
                    }
                    evidence.check(
                        f"{role} API inbox matches the persisted scheduled alerts",
                        own == expected_for_user,
                    )
                addresses = {
                    account.user["id"]: account.user["email"] for account in accounts.values()
                }
                evidence.check(
                    "all 20 SLA notification rows are marked emailed",
                    len(stored["notifications"]) == 20
                    and all(item["emailed_at"] for item in stored["notifications"]),
                )
                evidence.check(
                    "Mailpit contains exactly one matching email for every SLA notification",
                    len(mails) == 20
                    and all(
                        sum(
                            mail["ticket_id"] == item["ticket_id"]
                            and mail["subject"] == f"[SupportFlow] {item['title']}"
                            and mail["to"] == [addresses[item["user_id"]]]
                            for mail in mails
                        )
                        == 1
                        for item in stored["notifications"]
                    ),
                )
                met_read = request(client, "GET", f"/tickets/{met['id']}", owner)
                evidence.check(
                    "on-time reply and resolution remain met, without scheduled alerts",
                    met_read["sla"]["response"]["state"] == "met"
                    and met_read["sla"]["resolution"]["state"] == "met"
                    and not events(client, owner, met),
                )
                inactive_read = request(client, "GET", f"/tickets/{inactive['id']}", owner)
                evidence.check(
                    "disabled policy produces no SLA position or scheduled alerts",
                    inactive_read["sla"] is None and not events(client, owner, inactive),
                )
                foreign_read = client.get(
                    f"/api/v1/tickets/{assigned['id']}", headers=foreign.headers
                )
                evidence.check(
                    "another tenant cannot read the alerted ticket", foreign_read.status_code == 404
                )
                stable_at = datetime.now(UTC)
                stable_deadline = time.monotonic() + 30
                subsequent: list[dict[str, Any]] = []
                while time.monotonic() < stable_deadline:
                    subsequent = [
                        record
                        for record in logs("worker", stable_at)
                        if record.get("event") == "sla_sweep_complete"
                        and record.get("organization_id") == organization_id
                    ]
                    if len(subsequent) >= 2:
                        break
                    time.sleep(1)
                evidence.check(
                    "at least two further scheduled sweeps complete for this tenant",
                    len(subsequent) >= 2,
                )
                final = snapshot(owner, tickets)
                final_mail = mailbox(client, tickets)
                evidence.check(
                    "later sweeps create no duplicate events, notifications, or emails",
                    {item["id"] for item in final["events"]}
                    == {item["id"] for item in stored["events"]}
                    and {item["id"] for item in final["notifications"]} == expected_ids
                    and {mail["id"] for mail in final_mail} == {mail["id"] for mail in mails},
                )
                sweeps = [
                    record
                    for record in logs("worker", started)
                    if record.get("event") == "sla_sweep_complete"
                    and record.get("organization_id") == organization_id
                ]
                totals = {
                    key: sum(record.get(key, 0) for record in sweeps)
                    for key in ("warnings", "breaches", "notifications", "queued", "published")
                }
                evidence.check(
                    "worker logs confirm four warnings, four breaches and all 20 deliveries",
                    totals
                    == {
                        "warnings": 4,
                        "breaches": 4,
                        "notifications": 20,
                        "queued": 20,
                        "published": 20,
                    },
                )
                sends = [
                    record
                    for record in logs("beat", started)
                    if "Sending due task sla-deadline-sweep" in record.get("event", "")
                ]
                evidence.check("beat logs prove repeated automatic SLA dispatch", len(sends) >= 3)
                times = sorted(stamp(record["timestamp"]) for record in sends)
                gaps = [(second - first).total_seconds() for first, second in pairwise(times)]
                evidence.check(
                    "observed scheduler cadence matches the five-second override",
                    bool(gaps) and all(3 <= gap <= 10 for gap in gaps),
                )
                evidence.check("WSS observers stayed connected throughout", not socket_errors)
                evidence.details.update(
                    {
                        "database": final,
                        "emails": final_mail,
                        "scheduler_dispatches": len(sends),
                        "scheduler_gaps_seconds": gaps,
                        "subsequent_sweeps": subsequent,
                        "worker_sweeps": sweeps,
                        "worker_totals": totals,
                        "realtime_counts": {
                            role: sum(frame.get("ticket_id") in relevant_ids for frame in frames)
                            for role, frames in received.items()
                        },
                    }
                )
            finally:
                stop.set()
                for thread in threads:
                    thread.join(timeout=2)
    finally:
        for policy in policies:
            try:
                request(
                    client,
                    "PATCH",
                    f"/sla/policies/{policy['priority']}",
                    owner,
                    json={
                        key: policy[key]
                        for key in (
                            "response_time_minutes",
                            "resolution_time_minutes",
                            "warning_threshold_percent",
                            "is_active",
                        )
                    },
                )
            except Exception as exc:
                evidence.check(f"restore {policy['priority']} policy ({type(exc).__name__})", False)
        for row in tickets:
            try:
                current = request(client, "GET", f"/tickets/{row['id']}", owner)
                if current["status"] not in ("resolved", "closed"):
                    if current["status"] == "open":
                        request(
                            client,
                            "POST",
                            f"/tickets/{row['id']}/assign",
                            owner,
                            json={"assigned_agent_id": accounts["agent"].user["id"]},
                        )
                    request(
                        client,
                        "POST",
                        f"/tickets/{row['id']}/status",
                        owner,
                        json={"status": "in_progress"},
                    )
                    request(
                        client,
                        "POST",
                        f"/tickets/{row['id']}/status",
                        owner,
                        json={"status": "resolved"},
                    )
                request(client, "POST", f"/tickets/{row['id']}/close", owner)
            except Exception as exc:
                evidence.check(f"close test ticket #{row['number']} ({type(exc).__name__})", False)
        restored = request(client, "GET", "/sla/policies", owner)
        fields = (
            "priority",
            "response_time_minutes",
            "resolution_time_minutes",
            "warning_threshold_percent",
            "is_active",
        )
        evidence.check(
            "original test-tenant policies are restored",
            [{key: row[key] for key in fields} for row in restored]
            == [{key: row[key] for key in fields} for row in policies],
        )
        evidence.check(
            "all generated test tickets are closed",
            bool(tickets)
            and all(
                request(client, "GET", f"/tickets/{row['id']}", owner)["status"] == "closed"
                for row in tickets
            ),
        )


def main() -> None:
    evidence = Evidence()
    report = ROOT / "backend" / ".sla-scheduler-report.local"
    try:
        context = ssl.create_default_context(cafile=str(ROOT / "backend" / ".caddy-local-root.pem"))
        with httpx.Client(base_url=BASE, verify=context, timeout=20) as client:
            run(evidence, client, context)
    except Exception as exc:
        # HTTP/SSL errors may quote request metadata; the failed checks and class are enough.
        evidence.check(f"verification completed ({type(exc).__name__})", False)
    finally:
        passed = sum(check["passed"] for check in evidence.checks)
        failed = len(evidence.checks) - passed
        evidence.details.update(
            {
                "finished_at": datetime.now(UTC).isoformat(),
                "passed": passed,
                "failed": failed,
                "checks": evidence.checks,
            }
        )
        report.write_text(json.dumps(evidence.details, indent=2), encoding="utf-8")
        print(f"\n{passed} passed, {failed} failed. Report: {report}", flush=True)
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
