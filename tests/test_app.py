import tempfile
import unittest
import os
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import main


class SupportMateApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temp_dir.name) / "test.sqlite"
        self.database_patch = patch.object(main, "DATABASE_PATH", self.database_path)
        self.database_patch.start()
        self.auth_env_patch = patch.dict(
            os.environ,
            {
                "ADMIN_USERNAME": "test-admin",
                "ADMIN_PASSWORD": "a-strong-test-password",
                "SESSION_SECRET": "test-session-secret-with-more-than-32-characters",
            },
        )
        self.auth_env_patch.start()
        self.client_context = TestClient(main.app)
        self.client = self.client_context.__enter__()
        self.login_response = self.client.post(
            "/api/auth/login",
            json={"username": "test-admin", "password": "a-strong-test-password"},
        )
        self.assertEqual(self.login_response.status_code, 200)
        self.addCleanup(self.cleanup_client)

    def cleanup_client(self) -> None:
        self.client_context.__exit__(None, None, None)
        self.database_patch.stop()
        self.auth_env_patch.stop()
        self.temp_dir.cleanup()

    def admin_request(self, method: str, url: str, **kwargs):
        headers = dict(kwargs.pop("headers", {}))
        headers["X-CSRF-Token"] = self.client.cookies.get("support_mate_csrf")
        return self.client.request(method, url, headers=headers, **kwargs)

    def test_dashboard_and_static_frontend_are_served(self) -> None:
        page = self.client.get("/")
        script = self.client.get("/static/app.js")

        self.assertEqual(page.status_code, 200)
        self.assertIn("Support Mate", page.text)
        self.assertIn("Admin sign in", page.text)
        self.assertEqual(script.status_code, 200)
        self.assertIn("/api/chat", script.text)

    def test_admin_routes_require_login_and_csrf(self) -> None:
        anonymous_client = TestClient(main.app)
        self.assertEqual(anonymous_client.get("/api/dashboard").status_code, 401)
        self.assertEqual(anonymous_client.get("/api/knowledge").status_code, 401)

        missing_csrf = self.client.post(
            "/api/knowledge",
            json={"question": "Can I visit?", "answer": "Yes, visit us."},
        )
        self.assertEqual(missing_csrf.status_code, 403)

        dashboard = self.client.get("/api/dashboard")
        self.assertEqual(dashboard.status_code, 200)
        self.assertEqual(self.login_response.json()["authenticated"], True)
        self.assertIn("httponly", self.login_response.headers.get("set-cookie", "").lower())
        self.assertIn("samesite=strict", self.login_response.headers.get("set-cookie", "").lower())

    def test_missing_admin_configuration_fails_closed(self) -> None:
        with patch.dict(
            os.environ,
            {"ADMIN_USERNAME": "", "ADMIN_PASSWORD": "", "SESSION_SECRET": ""},
        ):
            response = self.client.get("/api/dashboard")

        self.assertEqual(response.status_code, 503)

    def test_admin_login_rejects_invalid_password_and_logout_clears_session(self) -> None:
        anonymous_client = TestClient(main.app)
        invalid_login = anonymous_client.post(
            "/api/auth/login",
            json={"username": "test-admin", "password": "wrong-password"},
        )
        self.assertEqual(invalid_login.status_code, 401)

        logout = self.admin_request("POST", "/api/auth/logout")
        self.assertEqual(logout.status_code, 200)
        self.assertFalse(self.client.get("/api/auth/session").json()["authenticated"])
        self.assertEqual(self.client.get("/api/dashboard").status_code, 401)

    def test_seeded_knowledge_base_answers_customer(self) -> None:
        response = self.client.post(
            "/api/chat",
            json={"message": "What are your business hours?", "customer_name": "Ari"},
        )

        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertIn("Monday through Friday", result["answer"])
        self.assertFalse(result["escalated"])
        self.assertIsNone(result["ticket_id"])

        anonymous_client = TestClient(main.app)
        conversation_response = anonymous_client.get(
            f"/api/conversations/{result['conversation_id']}",
            headers={"X-Conversation-Token": result["conversation_token"]},
        )
        self.assertEqual(conversation_response.status_code, 200)
        conversation = conversation_response.json()
        self.assertEqual(len(conversation["messages"]), 2)
        self.assertEqual(conversation["messages"][0]["role"], "customer")

    def test_conversation_id_alone_does_not_authorize_read_or_reply(self) -> None:
        anonymous_client = TestClient(main.app)
        first_message = anonymous_client.post(
            "/api/chat", json={"message": "What are your business hours?"}
        ).json()
        conversation_id = first_message["conversation_id"]
        token = first_message["conversation_token"]

        self.assertEqual(
            anonymous_client.get(f"/api/conversations/{conversation_id}").status_code,
            404,
        )
        self.assertEqual(
            anonymous_client.get(
                f"/api/conversations/{conversation_id}",
                headers={"X-Conversation-Token": "invalid"},
            ).status_code,
            404,
        )
        self.assertEqual(
            anonymous_client.post(
                "/api/chat",
                json={
                    "message": "What is your return policy?",
                    "conversation_id": conversation_id,
                    "conversation_token": "invalid",
                },
            ).status_code,
            404,
        )
        continuation = anonymous_client.post(
            "/api/chat",
            json={
                "message": "What is your return policy?",
                "conversation_id": conversation_id,
                "conversation_token": token,
            },
        )
        self.assertEqual(continuation.status_code, 200)

    def test_delivery_question_matches_shipping_article(self) -> None:
        response = self.client.post(
            "/api/chat", json={"message": "How long does delivery take?"}
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["escalated"])
        self.assertIn("3–5 business days", response.json()["answer"])

    def test_unanswered_question_creates_ticket_and_agent_can_resolve(self) -> None:
        chat = self.client.post(
            "/api/chat",
            json={
                "message": "Can you engrave initials onto a custom gift?",
                "customer_name": "Ari",
            },
        )

        self.assertEqual(chat.status_code, 200)
        result = chat.json()
        self.assertTrue(result["escalated"])
        self.assertIsNotNone(result["ticket_id"])
        self.assertIn("sent your question", result["answer"])

        dashboard = self.client.get("/api/dashboard").json()
        self.assertEqual(dashboard["stats"]["open_tickets"], 1)
        ticket = dashboard["tickets"][0]
        self.assertEqual(ticket["id"], result["ticket_id"])

        reply = self.admin_request(
            "POST",
            f"/api/tickets/{result['ticket_id']}/reply",
            json={"message": "We will check with our delivery team.", "resolve": True},
        )
        self.assertEqual(reply.status_code, 200)
        self.assertEqual(reply.json()["status"], "resolved")
        self.assertEqual(
            self.client.get("/api/dashboard").json()["stats"]["open_tickets"], 0
        )

        conversation = self.client.get(
            f"/api/conversations/{result['conversation_id']}",
            headers={"X-Conversation-Token": result["conversation_token"]},
        ).json()
        self.assertEqual(conversation["messages"][-1]["role"], "agent")

    def test_knowledge_articles_can_be_added_and_removed(self) -> None:
        created = self.admin_request(
            "POST",
            "/api/knowledge",
            json={
                "question": "Do you have a storefront?",
                "answer": "Yes, visit our downtown storefront.",
            },
        )
        self.assertEqual(created.status_code, 201)
        entry_id = created.json()["id"]

        answer = self.client.post(
            "/api/chat",
            json={"message": "Where is your storefront?"},
        )
        self.assertFalse(answer.json()["escalated"])
        self.assertIn("downtown", answer.json()["answer"])

        removed = self.admin_request("DELETE", f"/api/knowledge/{entry_id}")
        self.assertEqual(removed.status_code, 204)
        missing = self.admin_request("DELETE", f"/api/knowledge/{entry_id}")
        self.assertEqual(missing.status_code, 404)

    def test_invalid_ticket_status_is_rejected(self) -> None:
        chat = self.client.post(
            "/api/chat",
            json={"message": "Can I have custom engraving on my order?"},
        )
        ticket_id = chat.json()["ticket_id"]

        response = self.admin_request(
            "PATCH",
            f"/api/tickets/{ticket_id}", json={"status": "ignored"}
        )

        self.assertEqual(response.status_code, 422)


if __name__ == "__main__":
    unittest.main()
