"""Route tests for the app-level routes in app.py.

Covers the site root `/` redirect and guards that the API health route
still responds. Uses httpx.AsyncClient + ASGITransport because the installed
starlette/httpx versions are incompatible with TestClient (same approach as
test_authz.py). ASGITransport does not run lifespan, so no Firebase/SigLIP
warmup is triggered.

Run:  python test_app_routes.py   (from the backend/ directory)
"""
import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import httpx
import app


class _Client:
    """Minimal sync wrapper over httpx.AsyncClient + ASGITransport."""

    def get(self, path, **kw):
        async def _do():
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=app.app), base_url="http://test"
            ) as c:
                return await c.get(path, **kw)
        return asyncio.new_event_loop().run_until_complete(_do())


class AppRootRouteTest(unittest.TestCase):
    def setUp(self):
        self.client = _Client()

    def test_root_redirects_to_sign_page(self):
        resp = self.client.get("/", follow_redirects=False)
        self.assertEqual(resp.status_code, 307)
        self.assertEqual(resp.headers.get("location"), "/app/Sign.html")

    def test_root_redirect_follows_to_sign_page(self):
        resp = self.client.get("/", follow_redirects=True)
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/html", resp.headers.get("content-type", ""))

    def test_health_still_responds(self):
        resp = self.client.get("/health")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json().get("status"), "healthy")


if __name__ == "__main__":
    unittest.main()
