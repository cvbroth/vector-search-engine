"""Broker policy and HTTP boundary tests; no NAS or host socket is accessed."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import kb_policy_broker as broker


POLICY = {
    "schema_version": "1.0",
    "shared_scope": "family",
    "agents": {
        "main": {"private_scope": "chen", "shared": True},
        "chen": {"private_scope": "chen", "shared": True},
        "liang": {"private_scope": None, "shared": True},
        "ziling": {"private_scope": None, "shared": True},
    },
}


class MemorySocket:
    def __init__(self, request: bytes) -> None:
        self.input = io.BytesIO(request)
        self.output = io.BytesIO()

    def settimeout(self, _seconds: int) -> None:
        pass

    def makefile(self, _mode: str, _buffering: int) -> io.BytesIO:
        return self.input

    def sendall(self, content: bytes) -> None:
        self.output.write(content)


def request(policy: broker.BrokerPolicy, route: str, value: object | None = None,
            *, method: str = "POST") -> tuple[int, dict]:
    body = b"" if value is None else json.dumps(value, ensure_ascii=False).encode("utf-8")
    headers = f"{method} {route} HTTP/1.1\r\nHost: localhost\r\nContent-Length: {len(body)}\r\n"
    if method == "POST":
        headers += "Content-Type: application/json\r\n"
    connection = MemorySocket(headers.encode("ascii") + b"\r\n" + body)
    broker.BrokerHandler(connection, ("local", 0), SimpleNamespace(policy=policy))
    response_head, response_body = connection.output.getvalue().split(b"\r\n\r\n", 1)
    status = int(response_head.split(b"\r\n", 1)[0].split()[1])
    return status, json.loads(response_body)


def context(status: str, query: str, scope: str) -> dict:
    evidence = [] if status == "REJECT" else [{
        "scope": scope, "rank": 1, "fused_score": 0.03,
        "semantic_score": 0.75, "semantic_distance": 0.25,
        "lexical_match": True, "lexical_score": -0.1,
        "relevance_decision": status,
        "source_path": f"/srv/storage/knowledge/{'private/chen' if scope == 'chen' else 'shared/family'}/test.md",
        "filename": "test.md", "page": None, "chunk_index": 0,
        "text": "SECRET EVIDENCE CONTENT",
    }]
    return {
        "schema_version": "1.0", "query": query, "scopes": [scope],
        "retrieval_status": status, "evidence_count": len(evidence),
        "evidence": evidence,
    }


class BrokerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = broker.BrokerPolicy(
            "family", {agent: broker.AgentPolicy(rule["private_scope"], rule["shared"])
                       for agent, rule in POLICY["agents"].items()},
        )

    def test_mapping_and_denial(self) -> None:
        for agent in ("main", "chen"):
            self.assertEqual(self.policy.resolve(agent, "PRIVATE"), "chen")
        for agent in ("liang", "ziling"):
            self.assertIsNone(self.policy.resolve(agent, "PRIVATE"))
        for agent in POLICY["agents"]:
            self.assertEqual(self.policy.resolve(agent, "SHARED"), "family")
        self.assertIsNone(self.policy.resolve("unknown", "SHARED"))
        self.assertIsNone(self.policy.resolve("unknown", "PRIVATE"))

    def test_handler_routes_all_four_agents(self) -> None:
        resolved: list[str] = []

        def fake_forward(query: str, scope: str, top_k: int) -> dict:
            self.assertEqual(query, "问题")
            self.assertEqual(top_k, 3)
            resolved.append(scope)
            return context("ACCEPT", query, scope)

        with patch.object(broker, "forward_context", side_effect=fake_forward) as forward:
            for agent in ("main", "chen"):
                status, payload = request(self.policy, "/v1/private-context",
                                          {"agent_id": agent, "query": "问题", "top_k": 3})
                self.assertEqual(status, 200)
                self.assertEqual(payload["retrieval_status"], "ACCEPT")
                self.assertNotIn("scopes", payload)
                self.assertNotIn("scope", payload["evidence"][0])
                self.assertNotIn("source_path", payload["evidence"][0])
            for agent in ("liang", "ziling"):
                status, payload = request(self.policy, "/v1/private-context",
                                          {"agent_id": agent, "query": "问题", "top_k": 3})
                self.assertEqual(status, 403)
                self.assertEqual(payload, {"error": "private_knowledge_not_authorized"})
            for agent in POLICY["agents"]:
                status, payload = request(self.policy, "/v1/shared-context",
                                          {"agent_id": agent, "query": "问题", "top_k": 3})
                self.assertEqual(status, 200)
                self.assertNotIn("scopes", payload)
            self.assertEqual(forward.call_count, 6)
            self.assertEqual(resolved, ["chen", "chen", "family", "family", "family", "family"])

    def test_unknown_missing_agent_and_disabled_shared(self) -> None:
        with patch.object(broker, "forward_context") as forward:
            for endpoint in ("private", "shared"):
                status, _ = request(self.policy, f"/v1/{endpoint}-context",
                                    {"agent_id": "unknown", "query": "q"})
                self.assertEqual(status, 403)
                status, _ = request(self.policy, f"/v1/{endpoint}-context", {"query": "q"})
                self.assertEqual(status, 400)
            disabled = broker.BrokerPolicy("family", {"a": broker.AgentPolicy(None, False)})
            self.assertEqual(request(disabled, "/v1/shared-context", {"agent_id": "a", "query": "q"})[0], 403)
            forward.assert_not_called()

    def test_accept_uncertain_reject_and_backend_errors(self) -> None:
        for decision in ("ACCEPT", "UNCERTAIN", "REJECT"):
            with patch.object(broker, "forward_context", return_value=context(decision, "q", "family")):
                status, payload = request(self.policy, "/v1/shared-context", {"agent_id": "main", "query": "q"})
            self.assertEqual(status, 200)
            self.assertEqual(payload["retrieval_status"], decision)
            self.assertEqual(payload["evidence_count"], 0 if decision == "REJECT" else 1)
        for code in (500, 503, 504):
            with patch.object(broker, "forward_context", side_effect=broker.BackendError(code, "backend_failure")):
                status, payload = request(self.policy, "/v1/shared-context", {"agent_id": "main", "query": "q"})
            self.assertEqual(status, code)
            self.assertEqual(payload, {"error": "backend_failure"})

    def test_query_and_top_k_validation(self) -> None:
        invalid = [
            {"agent_id": "main", "query": ""},
            {"agent_id": "main", "query": "字" * 4097},
            {"agent_id": "main", "query": "q", "top_k": 0},
            {"agent_id": "main", "query": "q", "top_k": 11},
            {"agent_id": "main", "query": "q", "top_k": True},
            {"agent_id": "main", "query": "q", "scopes": ["chen"]},
        ]
        with patch.object(broker, "forward_context") as forward:
            for value in invalid:
                self.assertEqual(request(self.policy, "/v1/shared-context", value)[0], 400)
            forward.assert_not_called()

    def test_health_does_not_expose_policy(self) -> None:
        self.assertEqual(request(self.policy, "/health", method="GET"),
                         (200, {"status": "ok", "schema_version": "1.0"}))

    def test_audit_allow_deny_and_no_query_or_evidence(self) -> None:
        with patch.object(broker, "forward_context", return_value=context("ACCEPT", "secret query", "chen")):
            with self.assertLogs(broker.LOGGER, level="INFO") as logs:
                request(self.policy, "/v1/private-context", {"agent_id": "main", "query": "secret query"})
                request(self.policy, "/v1/private-context", {"agent_id": "liang", "query": "secret query"})
        entries = [json.loads(line.split("audit ", 1)[1]) for line in logs.output]
        self.assertEqual([entry["decision"] for entry in entries], ["ALLOW", "DENY"])
        self.assertEqual(entries[0]["resolved_scope"], "chen")
        self.assertEqual(entries[0]["retrieval_status"], "ACCEPT")
        for entry in entries:
            self.assertIn("request_id", entry)
            self.assertIn("latency_ms", entry)
            self.assertNotIn("secret query", json.dumps(entry))
            self.assertNotIn("SECRET EVIDENCE CONTENT", json.dumps(entry))

    def test_policy_file_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "policy.json"
            with self.assertRaises(broker.PolicyError):
                broker.load_policy(path)
            for content in (b"not json", b"{}", b'{"schema_version":"1.0","schema_version":"1.0"}'):
                path.write_bytes(content)
                with self.assertRaises(broker.PolicyError):
                    broker.load_policy(path)
            for invalid in [
                {**POLICY, "shared_scope": "unknown"},
                {**POLICY, "agents": {"a": {"private_scope": "liang", "shared": True}}},
                {**POLICY, "agents": {"a": {"private_scope": "family", "shared": True}}},
            ]:
                path.write_text(json.dumps(invalid), encoding="utf-8")
                with self.assertRaises(broker.PolicyError):
                    broker.load_policy(path)
            path.write_text(json.dumps(POLICY), encoding="utf-8")
            self.assertEqual(broker.load_policy(path).resolve("main", "PRIVATE"), "chen")

    def test_backend_response_validation(self) -> None:
        valid = context("ACCEPT", "q", "chen")
        self.assertEqual(broker.validate_backend_context(valid, "q", "chen", 5), valid)
        public = broker.to_model_context(valid, "q", "chen", 5)
        self.assertEqual(set(public), {
            "schema_version", "query", "retrieval_status", "evidence_count", "evidence",
        })
        self.assertEqual(set(public["evidence"][0]), {
            "rank", "fused_score", "semantic_score", "semantic_distance",
            "lexical_match", "lexical_score", "relevance_decision", "filename",
            "page", "chunk_index", "text",
        })
        self.assertNotIn("/srv/storage/knowledge/", json.dumps(public))
        self.assertEqual(broker.to_model_context(context("REJECT", "q", "chen"), "q", "chen", 5)["evidence"], [])
        for invalid in [
            {**valid, "retrieval_status": []},
            {**valid, "scopes": ["family"]},
            {**valid, "evidence_count": 2},
            {**valid, "evidence": [{**valid["evidence"][0], "relevance_decision": []}]},
        ]:
            with self.assertRaises(broker.BackendError):
                broker.validate_backend_context(invalid, "q", "chen", 5)
            with self.assertRaises(broker.BackendError):
                broker.to_model_context(invalid, "q", "chen", 5)

    def test_backend_nullable_scores_and_retrieval_status(self) -> None:
        for field in ("lexical_score", "semantic_score", "semantic_distance"):
            with self.subTest(field=field, value=None):
                valid = context("UNCERTAIN", "q", "family")
                valid["evidence"][0][field] = None
                if field == "lexical_score":
                    valid["evidence"][0]["lexical_match"] = False
                self.assertEqual(broker.validate_backend_context(valid, "q", "family", 5), valid)
                self.assertIsNone(broker.to_model_context(valid, "q", "family", 5)["evidence"][0][field])

            with self.subTest(field=field, value="float"):
                valid = context("UNCERTAIN", "q", "family")
                valid["evidence"][0][field] = 0.5
                self.assertEqual(broker.validate_backend_context(valid, "q", "family", 5), valid)

            for invalid_value in ("0.5", {}, True, float("inf")):
                with self.subTest(field=field, value=invalid_value):
                    invalid = context("UNCERTAIN", "q", "family")
                    invalid["evidence"][0][field] = invalid_value
                    with self.assertRaises(broker.BackendError):
                        broker.validate_backend_context(invalid, "q", "family", 5)

        mixed = context("ACCEPT", "q", "family")
        uncertain = {**mixed["evidence"][0], "rank": 2, "relevance_decision": "UNCERTAIN",
                     "lexical_match": False, "lexical_score": None}
        mixed["evidence"].append(uncertain)
        mixed["evidence_count"] = 2
        self.assertEqual(broker.validate_backend_context(mixed, "q", "family", 5), mixed)

        all_uncertain = context("UNCERTAIN", "q", "family")
        all_uncertain["evidence"][0].update(lexical_match=False, lexical_score=None)
        self.assertEqual(broker.validate_backend_context(all_uncertain, "q", "family", 5), all_uncertain)

        rejected = context("REJECT", "q", "family")
        self.assertEqual(broker.validate_backend_context(rejected, "q", "family", 5), rejected)

    def test_backend_transport_status_malformed_oversized_timeout(self) -> None:
        class FakeResponse:
            def __init__(self, status: int, body: bytes) -> None:
                self.status, self.body = status, body

            def read(self, limit: int) -> bytes:
                return self.body[:limit]

        class FakeConnection:
            response = FakeResponse(200, b"")
            error: Exception | None = None
            calls: list[tuple] = []

            def request(self, *args: object) -> None:
                self.calls.append(args)
                if self.error:
                    raise self.error

            def getresponse(self) -> FakeResponse:
                return self.response

            def close(self) -> None:
                pass

        fake = FakeConnection()
        with patch.object(broker, "UnixBackendConnection", return_value=fake):
            for scope in ("chen", "family"):
                fake.response = FakeResponse(200, json.dumps(context("ACCEPT", "q", scope)).encode("utf-8"))
                broker.forward_context("q", scope, 5)
                method, endpoint, body, _headers = fake.calls[-1]
                self.assertEqual((method, endpoint), ("POST", "/v1/context"))
                self.assertEqual(json.loads(body), {"query": "q", "scopes": [scope], "top_k": 5})
            for status, body, expected in [
                (500, b"{}", 500), (503, b"{}", 503),
                (200, b"not json", 502),
                (200, b"x" * (broker.MAX_RESPONSE_BYTES + 1), 502),
            ]:
                fake.response = FakeResponse(status, body)
                with self.assertRaises(broker.BackendError) as caught:
                    broker.forward_context("q", "chen", 5)
                self.assertEqual(caught.exception.status, expected)
            fake.error = TimeoutError()
            with self.assertRaises(broker.BackendError) as caught:
                broker.forward_context("q", "chen", 5)
            self.assertEqual(caught.exception.status, 504)


if __name__ == "__main__":
    unittest.main()
