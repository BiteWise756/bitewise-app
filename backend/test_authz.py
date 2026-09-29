"""Authorization tests for the NutriLens backend.

Requirement: authenticated endpoints must derive identity ONLY from the
verified Firebase ID token; client-supplied user IDs must never be trusted.

These tests use a fake Firestore + a stubbed token verifier, so no network
or Firebase credentials are needed. The fake is deliberately shared across
all modules so the stores behave like one real database.

Run:  python test_authz.py   (from the backend/ directory)
"""
import asyncio
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import httpx
import app
import utils.auth as auth_utils
from routes import (
    users as routes_users,
    shopping as routes_shopping,
    favorites as routes_favorites,
    challenges as routes_challenges,
    chat as routes_chat,
)
from services import auth_service, meal_service, nutrition_service


# ---------- fake Firestore ----------

class FakeDoc:
    def __init__(self, col, doc_id, data):
        self._col = col
        self.id = doc_id
        self._data = data

    @property
    def exists(self):
        return self._data is not None

    @property
    def reference(self):
        return FakeDocRef(self._col, self.id)

    def to_dict(self):
        return dict(self._data) if self._data is not None else None


class FakeDocRef:
    def __init__(self, col, doc_id):
        self._col = col
        self.id = doc_id

    def get(self):
        data = self._col._store.get(self.id)
        return FakeDoc(self._col, self.id, dict(data) if data is not None else None)

    def set(self, data):
        self._col._store[self.id] = dict(data)
        return None

    def update(self, data):
        d = dict(self._col._store.get(self.id) or {})
        d.update(data)
        self._col._store[self.id] = d
        return None

    def delete(self):
        self._col._store.pop(self.id, None)
        return None


def _filter_value(f):
    """Pull (field, op, value) out of .where() args/kwargs, incl. FieldFilter."""
    for k in ("filter",):
        val = f.get(k)
        if val is not None:
            field = getattr(val, "field_path", None) or getattr(val, "field", None)
            op = getattr(val, "op_string", None) or getattr(val, "op", None) or "=="
            value = getattr(val, "value", None)
            return field, op, value
    return None


class FakeQuery:
    def __init__(self, col, filters):
        self._col = col
        self._filters = filters

    def where(self, *args, **kwargs):
        parsed = _filter_value(kwargs)
        if parsed is None and len(args) >= 3:
            parsed = (args[0], args[1], args[2])
        self._filters.append(parsed)
        return self

    def limit(self, n):
        return self

    def order_by(self, *a, **k):
        return self

    def _docs(self):
        out = []
        for doc_id, data in self._col._store.items():
            ok = True
            if data is not None:
                for field, op, value in self._filters:
                    if field is None:
                        continue
                    actual = data.get(field)
                    if op in ("==",):
                        if actual != value:
                            ok = False
                    elif op in ("!=",):
                        if actual == value:
                            ok = False
                    elif op in (">", ">="):
                        if not (actual is not None and actual > (value if op == ">" else value - 1 if isinstance(value, (int, float)) else value)):
                            ok = False
                    elif op in ("<", "<="):
                        pass
            if ok:
                out.append(FakeDoc(self._col, doc_id, data))
        return out

    def stream(self):
        return iter(self._docs())

    def get(self):
        return self._docs()


class FakeCol:
    def __init__(self, store):
        self._store = store
        self._auto = 0

    def document(self, doc_id=None):
        if doc_id is None:
            self._auto += 1
            doc_id = f"auto_{self._auto}"
        return FakeDocRef(self, doc_id)

    def add(self, data):
        ref = self.document()
        ref.set(data)
        return ref

    def where(self, *args, **kwargs):
        return FakeQuery(self, [])

    def order_by(self, *a, **k):
        return self


class FakeDB:
    def __init__(self):
        self.cols = {}

    def collection(self, name):
        if name not in self.cols:
            self.cols[name] = FakeCol({})
        return self.cols[name]

    def store(self, name):
        return self.collection(name)._store


# ---------- helpers ----------

TOKEN_USER = "token-user1"
TOKEN_USER2 = "token-user2"
TOKEN_USER3 = "token-user3"
TOKEN_DOC = "token-doc1"
UID1, UID2, UID3, DOC_UID = "USER-1", "USER-2", "USER-3", "DOC-1"
VALID_TOKENS = {
    TOKEN_USER: {"uid": UID1},
    TOKEN_USER2: {"uid": UID2},
    TOKEN_USER3: {"uid": UID3},
    TOKEN_DOC: {"uid": DOC_UID},
}


class _Client:
    """Minimal sync wrapper over httpx.AsyncClient + ASGITransport.
    Starlette's TestClient is incompatible with the installed httpx."""

    def _run(self, method, path, **kw):
        async def _do():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app.app), base_url="http://test"
            ) as c:
                return await c.request(method, path, **kw)
        return asyncio.new_event_loop().run_until_complete(_do())

    def get(self, path, **kw):
        return self._run("GET", path, **kw)

    def post(self, path, **kw):
        return self._run("POST", path, **kw)

    def put(self, path, **kw):
        return self._run("PUT", path, **kw)

    def delete(self, path, **kw):
        return self._run("DELETE", path, **kw)

    def request(self, method, path, **kw):
        return self._run(method, path, **kw)


class AuthzTest(unittest.TestCase):
    def setUp(self):
        self.db = FakeDB()
        self.client = _Client()
        self.token = TOKEN_USER
        self.user = UID1

        for uid in (UID1, UID2, UID3):
            self.db.store("users")[uid] = {
                "uid": uid, "email": f"{uid}@example.com", "display_id": f"USR-{uid}",
                "accountType": "user", "emailVerified": True,
            }
        self.db.store("users")[DOC_UID] = {
            "uid": DOC_UID, "email": "doc@example.com", "display_id": "USR-DOC1",
            "accountType": "doctor", "emailVerified": True,
        }
        # App-created meal_history doc and shopping/favorites items
        self.db.store("meal_history")["M-mine"] = {"userId": UID1, "foodName": "Oats", "calories": 200}
        self.db.store("meal_history")["M-other"] = {"userId": UID2, "foodName": "Burger", "calories": 700}
        self.db.store("shopping_lists")["I-mine"] = {"userId": UID1, "name": "Apples", "done": False}
        self.db.store("shopping_lists")["I-other"] = {"userId": UID2, "name": "Candy", "done": False}
        self.db.store("favorites")["F-mine"] = {"userId": UID1, "name": "Rice Bowl"}
        self.db.store("favorites")["F-other"] = {"userId": UID2, "name": "Pizza"}

        self._patchers = [
            mock.patch.object(auth_utils, "get_db", return_value=self.db),
            mock.patch.object(auth_utils, "verify_token", side_effect=self._verify),
            mock.patch.object(routes_users, "get_db", return_value=self.db),
            mock.patch.object(routes_shopping, "get_db", return_value=self.db),
            mock.patch.object(routes_favorites, "get_db", return_value=self.db),
            mock.patch.object(routes_challenges, "get_db", return_value=self.db),
            mock.patch.object(routes_chat, "get_db", return_value=self.db),
            mock.patch.object(auth_service, "get_db", return_value=self.db),
            mock.patch.object(meal_service, "get_db", return_value=self.db),
            mock.patch.object(nutrition_service, "get_db", return_value=self.db),
            mock.patch.object(routes_chat, "generate_chat_reply", return_value="AI reply"),
            mock.patch("services.gemini_service._call_gemini", return_value="Possible diagnosis: test"),
        ]
        for p in self._patchers:
            p.start()
            self.addCleanup(p.stop)

    def _verify(self, token):
        return VALID_TOKENS.get(token)

    def _headers(self):
        return {"Authorization": f"Bearer {self.token}"}

    def _headers_for(self, token):
        return {"Authorization": f"Bearer {token}"}

    # ---------- requirement: missing token -> 401 everywhere protected ----------

    def test_missing_token_is_401_on_all_protected_endpoints(self):
        cases = [
            ("GET", "/auth/profile/USER-1", None),
            ("PUT", "/auth/profile/USER-1", {"displayName": "X"}),
            ("GET", "/meal/history", None),
            ("GET", "/nutrition/dashboard/USER-1", None),
            ("POST", "/nutrition/water", None),
            ("POST", "/chat/", {"message": "hi"}),
            ("GET", "/shopping/USER-1", None),
            ("POST", "/shopping/USER-1", {"name": "Milk"}),
            ("PUT", "/shopping/M-mine", {"done": True}),
            ("DELETE", "/shopping/M-mine", None),
            ("GET", "/favorites/USER-1", None),
            ("DELETE", "/favorites/F-mine", None),
            ("POST", "/challenges/join/USER-1", {"challengeId": "no-sugar"}),
            ("GET", "/challenges/joined/USER-1", None),
            ("GET", "/api/users/USER-1", None),
            ("POST", "/api/users/USER-1/doctor-notes", {"note": "hi"}),
            ("POST", "/doctor/analyze", {"text": "blood test"}),
            ("GET", "/doctor/profile", None),
        ]
        for method, path, body in cases:
            res = self.client.request(method, path, json=body)
            self.assertEqual(res.status_code, 401, f"{method} {path} should be 401 without a token")

    def test_invalid_token_is_401(self):
        res = self.client.get("/meal/history", headers={"Authorization": "Bearer garbage"})
        self.assertEqual(res.status_code, 401)

    def test_public_endpoints_still_work_without_token(self):
        res = self.client.get("/meal/alternatives")
        self.assertEqual(res.status_code, 200)
        res = self.client.post("/nutrition/water?userId=USER-9&amount=250")
        self.assertEqual(res.status_code, 401)  # water is NOT public

    # ---------- requirement: cross-user access denied ----------

    def test_other_users_user_scoped_reads_are_404(self):
        self.token = TOKEN_USER
        for path in (
            "/api/users/USER-2",
            "/api/users/USER-2/progress",
            "/api/users/USER-2/history",
            "/api/users/USER-2/ai-history",
            "/nutrition/dashboard/USER-2",
            "/nutrition/progress/USER-2",
            "/nutrition/water/USER-2",
            "/nutrition/weight/USER-2",
            "/nutrition/report/USER-2",
            "/auth/profile/USER-2",
        ):
            res = self.client.get(path, headers=self._headers())
            self.assertEqual(res.status_code, 404, f"GET {path} should be 404 for another user")

    def test_other_users_user_scoped_writes_are_403(self):
        self.token = TOKEN_USER
        cases = [
            ("PUT", "/auth/profile/USER-2", {"displayName": "Hacker"}),
            ("GET", "/shopping/USER-2", None),
            ("POST", "/shopping/USER-2", {"name": "Hack"}),
            ("GET", "/favorites/USER-2", None),
            ("POST", "/favorites/USER-2", {"name": "Hack"}),
            ("POST", "/challenges/join/USER-2", {"challengeId": "no-sugar"}),
            ("DELETE", "/challenges/join/USER-2?challengeId=no-sugar", None),
            ("GET", "/challenges/joined/USER-2", None),
            ("POST", "/chat/", {"message": "hi", "userId": "USER-2"}),
            ("DELETE", "/meal/history/M-other", None),
            ("PUT", "/shopping/I-other", {"done": True}),
            ("DELETE", "/shopping/I-other", None),
            ("DELETE", "/favorites/F-other", None),
        ]
        for method, path, body in cases:
            res = self.client.request(method, path, json=body, headers=self._headers())
            self.assertEqual(res.status_code, 403, f"{method} {path} should be 403 for another user")

    def test_meal_history_query_rejects_foreign_userid_param(self):
        self.token = TOKEN_USER
        res = self.client.get("/meal/history?userId=USER-2", headers=self._headers())
        self.assertEqual(res.status_code, 403)
        res = self.client.get("/meal/history?userId=USER-1", headers=self._headers())
        self.assertEqual(res.status_code, 200)

    def test_nutrition_writes_reject_foreign_query_uid_and_use_token_uid(self):
        self.token = TOKEN_USER
        res = self.client.post(
            "/nutrition/water?userId=USER-2&amount=250", headers=self._headers()
        )
        self.assertEqual(res.status_code, 403, "foreign userId in query must be rejected")
        self.assertEqual(self.db.store("water_logs"), {}, "no log may be written for a foreign user")
        res = self.client.post("/nutrition/water?amount=250", headers=self._headers())
        self.assertEqual(res.status_code, 200)
        logs = self.db.store("water_logs")
        self.assertTrue(all(v.get("userId") == UID1 for v in logs.values()), logs)

    def test_challenge_join_stores_token_uid(self):
        self.token = TOKEN_USER
        res = self.client.post(
            "/challenges/join/USER-1", json={"challengeId": "no-sugar"}, headers=self._headers()
        )
        self.assertEqual(res.status_code, 200)
        for v in self.db.store("user_challenges").values():
            self.assertEqual(v["userId"], UID1)

    def test_chat_stores_token_uid(self):
        self.token = TOKEN_USER
        res = self.client.post("/chat/", json={"message": "hi"}, headers=self._headers())
        self.assertEqual(res.status_code, 200)
        entries = self.db.store("ai_history")
        self.assertTrue(all(v.get("userId") == UID1 for v in entries.values()), entries)

    def test_register_bootstraps_at_token_uid_only(self):
        self.token = TOKEN_USER3
        res = self.client.post(
            "/api/users/register",
            json={"email": "ghost@example.com", "displayName": "Ghost"},
            headers=self._headers(),
        )
        self.assertEqual(res.status_code, 200, res.text)
        self.assertIn(UID3, self.db.store("users"))

    def test_delete_own_meal_allowed(self):
        self.token = TOKEN_USER
        res = self.client.delete("/meal/history/M-mine", headers=self._headers())
        self.assertEqual(res.status_code, 200)
        self.assertNotIn("M-mine", self.db.store("meal_history"))

    def test_delete_own_shopping_favorite_allowed(self):
        self.token = TOKEN_USER
        res = self.client.delete("/shopping/I-mine", headers=self._headers())
        self.assertEqual(res.status_code, 200)
        res = self.client.delete("/favorites/F-mine", headers=self._headers())
        self.assertEqual(res.status_code, 200)

    def test_own_reads_succeed(self):
        self.token = TOKEN_USER
        res = self.client.get("/api/users/USER-1", headers=self._headers())
        self.assertEqual(res.status_code, 200)
        res = self.client.get("/shopping/USER-1", headers=self._headers())
        self.assertEqual(res.status_code, 200)
        res = self.client.get("/auth/profile/USER-1", headers=self._headers())
        self.assertEqual(res.status_code, 200)

    def test_profile_update_cannot_change_role(self):
        self.token = TOKEN_USER
        res = self.client.put(
            "/auth/profile/USER-1",
            json={"displayName": "Renamed", "accountType": "doctor"},
            headers=self._headers(),
        )
        self.assertEqual(res.status_code, 200)
        doc = self.db.store("users")[UID1]
        self.assertEqual(doc["displayName"], "Renamed")
        self.assertEqual(doc["accountType"], "user", "accountType must be immutable via profile update")

    # ---------- requirement: doctor role gates ----------

    def test_doctor_analyze_requires_doctor_role(self):
        self.token = TOKEN_USER
        res = self.client.post("/doctor/analyze", json={"text": "test"}, headers=self._headers())
        self.assertEqual(res.status_code, 403)
        self.token = TOKEN_USER2
        res = self.client.post("/doctor/analyze", json={"text": "test"}, headers=self._headers())
        self.assertEqual(res.status_code, 403)

    def test_doctor_analyze_allowed_for_doctor(self):
        self.token = TOKEN_DOC
        res = self.client.post("/doctor/analyze", json={"text": "blood test"}, headers=self._headers())
        self.assertEqual(res.status_code, 200, res.text)
        self.assertTrue(res.json().get("success"))

    def test_doctor_notes_require_doctor_role(self):
        self.token = TOKEN_USER
        res = self.client.post(
            "/api/users/USER-2/doctor-notes",
            json={"userId": "USER-2", "note": "Looks fine", "doctorId": "fake-doctor"},
            headers=self._headers(),
        )
        self.assertEqual(res.status_code, 403)

    def test_doctor_notes_use_token_doctorId(self):
        self.token = TOKEN_DOC
        res = self.client.post(
            "/api/users/USER-2/doctor-notes",
            json={"userId": "USER-2", "note": "Looks fine", "doctorId": "fake-doctor"},
            headers=self._headers(),
        )
        self.assertEqual(res.status_code, 200, res.text)
        notes = self.db.store("doctor_notes")
        self.assertTrue(notes, "expected a stored doctor note")
        for v in notes.values():
            self.assertEqual(v["doctorId"], DOC_UID, "doctorId must come from the token")
            self.assertEqual(v["userId"], UID2)

    def test_doctor_can_view_patient_data(self):
        self.token = TOKEN_DOC
        res = self.client.get("/api/users/USER-2", headers=self._headers())
        self.assertEqual(res.status_code, 200)
        res = self.client.get("/api/users/USER-2/doctor-notes", headers=self._headers())
        self.assertEqual(res.status_code, 200)

    def test_doctor_profile_own_only(self):
        self.token = TOKEN_DOC
        res = self.client.get("/doctor/profile", headers=self._headers())
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.json()["data"]["uid"], DOC_UID)
        res = self.client.get("/doctor/profile?uid=USER-2", headers=self._headers())
        self.assertEqual(res.status_code, 404)
        self.token = TOKEN_USER
        res = self.client.get("/doctor/profile", headers=self._headers())
        self.assertEqual(res.status_code, 403)

    def test_regular_user_cannot_read_notes_of_patient(self):
        self.token = TOKEN_USER
        res = self.client.get("/api/users/USER-2/doctor-notes", headers=self._headers())
        self.assertEqual(res.status_code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)