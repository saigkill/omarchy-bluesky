#!/usr/bin/env python3
"""Tests for bluesky_helper.py.

The helper is the only component that ever sees a credential, so these tests
aim at four things: that the app password and the tokens never reach a process
argument or the helper's output, that a token only ever goes over TLS to the
server that issued it, that the session survives an expired access token and
ends cleanly when the refresh token is refused, and that the state file stays
unreadable for other users.

They run the helper against a local TLS server that plays a PDS.

Run with: python3 -m unittest discover -s tests -v
"""

import base64
import contextlib
import io
import json
import os
import shutil
import stat
import subprocess
import ssl
import sys
import tempfile
import threading
import time
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
HELPER = os.path.join(ROOT, "bluesky_helper.py")

sys.dont_write_bytecode = True
sys.path.insert(0, ROOT)
import bluesky_helper as helper  # noqa: E402

PASSWORD = "abcd-efgh-ijkl-mnop"
DID = "did:plc:testuser1234567890"
OTHER_DID = "did:plc:someoneelse12345"


def jwt(label, exp):
    """A token-shaped string with an exp claim. The fake server compares the
    whole string; the helper only ever reads exp to schedule a refresh."""
    def part(data):
        return base64.urlsafe_b64encode(json.dumps(data).encode()).rstrip(b"=").decode()
    return part({"alg": "none"}) + "." + part({"exp": int(exp), "sub": label}) + ".sig-" + label


def fresh(label):
    return jwt(label, time.time() + 3600)


_CERT_CACHE = {}


def self_signed_cert():
    """One certificate for the whole run. The helper trusts it through
    SSL_CERT_FILE, which keeps a "disable verification" switch out of the
    production code."""
    if not _CERT_CACHE:
        directory = tempfile.mkdtemp()
        cert = os.path.join(directory, "cert.pem")
        key = os.path.join(directory, "key.pem")
        subprocess.run(
            ["openssl", "req", "-x509", "-newkey", "rsa:2048", "-keyout", key,
             "-out", cert, "-days", "1", "-nodes", "-subj", "/CN=localhost",
             "-addext", "subjectAltName=DNS:localhost"],
            check=True, capture_output=True)
        _CERT_CACHE["cert"] = cert
        _CERT_CACHE["key"] = key
    return _CERT_CACHE["cert"], _CERT_CACHE["key"]


class FakePds:
    """A local TLS server that answers like a PDS and records every request."""

    def __init__(self):
        self.requests = []
        self.access = fresh("access-1")
        self.refresh = fresh("refresh-1")
        self.refresh_refused = False
        # When set, the next authenticated GET answers ExpiredToken once, the
        # way a PDS answers an access token that has run out.
        self.expire_once = False
        self.redirect_to = ""
        self.huge = False
        self.did_doc_endpoint = None
        self.notifications = []
        self.posts = {}
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def answer(self, status, data):
                payload = data if isinstance(data, bytes) else json.dumps(data).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                try:
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    # The helper hangs up on an oversized body, which is the
                    # point of that test, not a failure of the server.
                    pass

            def error(self, status, name):
                self.answer(status, {"error": name, "message": name})

            def record(self, body=b""):
                parts = urllib.parse.urlsplit(self.path)
                entry = {
                    "method": self.command,
                    "method_name": parts.path.rsplit("/", 1)[-1],
                    "query": urllib.parse.parse_qs(parts.query),
                    "headers": dict(self.headers),
                    "raw": body,
                }
                try:
                    entry["json"] = json.loads(body) if body else None
                except ValueError:
                    entry["json"] = None
                outer.requests.append(entry)
                return entry

            def authorized(self, token):
                return self.headers.get("Authorization") == "Bearer " + token

            def do_GET(self):
                entry = self.record()
                name = entry["method_name"]
                if outer.redirect_to:
                    self.send_response(302)
                    self.send_header("Location", outer.redirect_to)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if not self.authorized(outer.access):
                    return self.error(400, "ExpiredToken")
                if outer.expire_once:
                    outer.expire_once = False
                    return self.error(400, "ExpiredToken")
                if outer.huge:
                    return self.answer(200, b'{"feed":[' + b"1," * (10 * 1024 * 1024) + b"1]}")
                if name in ("app.bsky.feed.getTimeline", "app.bsky.feed.getFeed"):
                    return self.answer(200, {"feed": [], "cursor": "next-page"})
                if name == "app.bsky.notification.listNotifications":
                    return self.answer(200, {"notifications": outer.notifications, "cursor": "n2"})
                if name == "app.bsky.feed.getPosts":
                    uris = entry["query"].get("uris", [])
                    return self.answer(200, {"posts": [outer.posts[u] for u in uris if u in outer.posts]})
                if name == "com.atproto.identity.resolveHandle":
                    handle = entry["query"].get("handle", [""])[0]
                    if handle == "alice.example.org":
                        return self.answer(200, {"did": "did:plc:alice1234567890"})
                    return self.error(400, "InvalidRequest")
                return self.error(404, "MethodNotImplemented")

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                entry = self.record(self.rfile.read(length))
                name = entry["method_name"]
                body = entry["json"] or {}
                if name == "com.atproto.server.createSession":
                    if body.get("password") == "2fa-required":
                        return self.error(401, "AuthFactorTokenRequired")
                    if body.get("password") != PASSWORD:
                        return self.error(401, "AuthenticationRequired")
                    session = {"did": DID, "handle": "tester.example.org",
                               "accessJwt": outer.access, "refreshJwt": outer.refresh}
                    if outer.did_doc_endpoint is not None:
                        session["didDoc"] = {"id": DID, "service": [{
                            "id": "#atproto_pds", "type": "AtprotoPersonalDataServer",
                            "serviceEndpoint": outer.did_doc_endpoint}]}
                    return self.answer(200, session)
                if name == "com.atproto.server.refreshSession":
                    if outer.refresh_refused or not self.authorized(outer.refresh):
                        return self.error(400, "ExpiredToken")
                    outer.access = fresh("access-%d" % len(outer.requests))
                    outer.refresh = fresh("refresh-%d" % len(outer.requests))
                    return self.answer(200, {"did": DID, "handle": "tester.example.org",
                                             "accessJwt": outer.access, "refreshJwt": outer.refresh})
                if name == "com.atproto.server.deleteSession":
                    return self.answer(200, {})
                if not self.authorized(outer.access):
                    return self.error(400, "ExpiredToken")
                if name == "com.atproto.repo.createRecord":
                    collection = body.get("collection", "")
                    return self.answer(200, {"uri": "at://%s/%s/3kcreated" % (DID, collection),
                                             "cid": "bafyreicreated123"})
                if name == "com.atproto.repo.deleteRecord":
                    return self.answer(200, {})
                if name == "com.atproto.repo.uploadBlob":
                    return self.answer(200, {"blob": {
                        "$type": "blob", "ref": {"$link": "bafkreiuploaded1234"},
                        "mimeType": self.headers.get("Content-Type"), "size": length}})
                if name in ("app.bsky.bookmark.createBookmark", "app.bsky.bookmark.deleteBookmark"):
                    return self.answer(200, {})
                return self.error(404, "MethodNotImplemented")

            def log_message(self, *args):
                pass

        cert, key = self_signed_cert()
        self.cert = cert
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        self.server.socket = context.wrap_socket(self.server.socket, server_side=True)
        self.base = "https://localhost:%d" % self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

    def calls(self, name):
        return [r for r in self.requests if r["method_name"] == name]


class HelperTestCase(unittest.TestCase):
    def setUp(self):
        self.state_dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.state_dir, True)
        self.state = os.path.join(self.state_dir, "omarchy-bluesky", "auth.json")
        self.pds = FakePds()
        self.addCleanup(self.pds.stop)
        self.environ = {"BLUESKY_AUTH_FILE": self.state, "SSL_CERT_FILE": self.pds.cert,
                        "BLUESKY_IMAGE_TOOL": "", "PYTHONDONTWRITEBYTECODE": "1"}
        saved = {key: os.environ.get(key) for key in self.environ}

        def restore():
            for key, value in saved.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

        self.addCleanup(restore)
        os.environ.update(self.environ)

    def run_helper(self, *args, env=None):
        environment = dict(os.environ)
        environment.update(self.environ)
        environment.update(env or {})
        return subprocess.run([sys.executable, HELPER, *args],
                              capture_output=True, env=environment, timeout=60)

    def login(self):
        result = self.run_helper("login", self.pds.base, "tester.example.org",
                                 env={"BLUESKY_APP_PASSWORD": PASSWORD})
        self.assertEqual(result.returncode, 0, result.stderr)
        return result

    def write_session(self, access=None, refresh=None):
        auth = helper.empty_auth()
        auth.update(service=self.pds.base, pds=self.pds.base, handle="tester.example.org",
                    did=DID, accessJwt=access or self.pds.access,
                    refreshJwt=refresh or self.pds.refresh)
        helper.write_state({"auth": auth})

    def stored(self):
        with open(self.state, encoding="utf-8") as handle:
            return json.load(handle)["auth"]


# ------------------------------------------------------------------ secrets


class SecretsStayInTheHelper(HelperTestCase):
    def test_the_password_goes_to_the_server_and_nowhere_else(self):
        result = self.login()
        sent = self.pds.calls("com.atproto.server.createSession")[0]["json"]
        self.assertEqual(sent, {"identifier": "tester.example.org", "password": PASSWORD})
        self.assertNotIn(PASSWORD.encode(), result.stdout + result.stderr)
        with open(self.state, "rb") as handle:
            self.assertNotIn(PASSWORD.encode(), handle.read(), "the password must not be stored")

    def test_login_answers_with_a_boolean_instead_of_the_tokens(self):
        result = self.login()
        payload = json.loads(result.stdout)
        self.assertEqual(payload["auth"]["hasSession"], True)
        self.assertEqual(payload["auth"]["did"], DID)
        self.assertNotIn(self.pds.access.encode(), result.stdout)
        self.assertNotIn(self.pds.refresh.encode(), result.stdout)
        self.assertNotIn(b"Jwt", result.stdout)

    def test_load_reports_a_boolean_instead_of_the_tokens(self):
        self.write_session()
        result = self.run_helper("load")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["auth"]["hasSession"])
        self.assertNotIn(self.pds.access.encode(), result.stdout)
        self.assertEqual(sorted(payload["auth"]), ["did", "handle", "hasSession", "service"])

    def test_a_password_on_the_command_line_is_not_accepted(self):
        result = self.run_helper("login", self.pds.base, "tester.example.org", PASSWORD)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.pds.requests, [])

    def test_login_without_the_environment_variable_fails(self):
        result = self.run_helper("login", self.pds.base, "tester.example.org")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"missing_password", result.stderr)
        self.assertEqual(self.pds.requests, [])

    def test_feed_output_carries_no_token(self):
        self.write_session()
        result = self.run_helper("timeline")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(self.pds.access.encode(), result.stdout + result.stderr)


class LoginErrors(HelperTestCase):
    def test_a_wrong_password_is_reported_and_nothing_is_stored(self):
        result = self.run_helper("login", self.pds.base, "tester.example.org",
                                 env={"BLUESKY_APP_PASSWORD": "wrong"})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"BLUESKY_ERROR:invalid_credentials", result.stderr)
        self.assertEqual(result.stdout, b"")
        self.assertFalse(os.path.exists(self.state))

    def test_an_account_password_with_two_factor_asks_for_an_app_password(self):
        result = self.run_helper("login", self.pds.base, "tester.example.org",
                                 env={"BLUESKY_APP_PASSWORD": "2fa-required"})
        self.assertIn(b"BLUESKY_ERROR:auth_factor_required", result.stderr)

    def test_a_leading_at_sign_is_dropped(self):
        self.run_helper("login", self.pds.base, "@tester.example.org",
                        env={"BLUESKY_APP_PASSWORD": PASSWORD})
        sent = self.pds.calls("com.atproto.server.createSession")[0]["json"]
        self.assertEqual(sent["identifier"], "tester.example.org")


# --------------------------------------------------------------------- TLS


class TokensStayOnTls(HelperTestCase):
    def test_plaintext_servers_are_refused(self):
        for service in ("http://pds.example.org", "http://127.0.0.1.evil.example"):
            with self.assertRaises(helper.HelperError) as caught:
                helper.normalize_service(service)
            self.assertEqual(caught.exception.message, "insecure_service")

    def test_userinfo_and_other_schemes_are_refused(self):
        for service in ("https://bsky.social@evil.example", "bsky.social@evil.example",
                        "javascript:alert(1)", "ftp://pds.example.org"):
            with self.assertRaises(helper.HelperError, msg=service):
                helper.normalize_service(service)

    def test_hosts_and_ports_are_accepted(self):
        self.assertEqual(helper.normalize_service(""), "https://bsky.social")
        self.assertEqual(helper.normalize_service("pds.example.org:8443"), "https://pds.example.org:8443")
        self.assertEqual(helper.normalize_service("https://PDS.example.org/xrpc"), "https://pds.example.org")
        self.assertEqual(helper.normalize_service("http://localhost:2583"), "http://localhost:2583")

    def test_a_lookalike_host_is_not_loopback(self):
        self.assertFalse(helper.is_loopback("127.0.0.1.evil.example"))
        self.assertTrue(helper.is_loopback("127.0.0.5"))
        self.assertTrue(helper.is_loopback("::1"))

    def test_the_pds_from_the_did_document_is_used(self):
        # The entryway hands out the session; the account's own PDS is where
        # requests then go. Here both are the fake server, under two names.
        self.pds.did_doc_endpoint = self.pds.base.replace("localhost", "127.0.0.1")
        self.login()
        self.assertEqual(self.stored()["pds"], self.pds.did_doc_endpoint)

    def test_a_plaintext_pds_in_the_did_document_is_ignored(self):
        self.pds.did_doc_endpoint = "http://pds.evil.example"
        self.login()
        self.assertEqual(self.stored()["pds"], self.pds.base)

    def test_a_cross_origin_redirect_is_refused(self):
        self.write_session()
        self.pds.redirect_to = "https://evil.example/steal"
        result = self.run_helper("timeline")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"cross_origin_redirect", result.stderr)
        self.assertEqual(result.stdout, b"")

    def test_an_oversized_response_is_refused_instead_of_buffered(self):
        self.write_session()
        self.pds.huge = True
        result = self.run_helper("timeline")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"response_too_large", result.stderr)

    def test_method_names_cannot_walk_out_of_xrpc(self):
        for name in ("../admin", "app.bsky/../x", "app", "app.bsky.feed.getTimeline?x=1", ""):
            with self.assertRaises(helper.HelperError, msg=name):
                helper.check_nsid(name)


# ---------------------------------------------------------------- sessions


class SessionLifecycle(HelperTestCase):
    def test_an_expired_access_token_is_refreshed_before_the_request(self):
        self.write_session(access=jwt("stale", time.time() - 10))
        result = self.run_helper("timeline")
        self.assertEqual(result.returncode, 0, result.stderr)
        names = [r["method_name"] for r in self.pds.requests]
        self.assertEqual(names, ["com.atproto.server.refreshSession", "app.bsky.feed.getTimeline"])
        self.assertEqual(self.stored()["refreshJwt"], self.pds.refresh, "the rotated token is kept")

    def test_a_token_the_server_calls_expired_is_refreshed_and_retried_once(self):
        self.write_session()
        self.pds.expire_once = True
        result = self.run_helper("timeline")
        self.assertEqual(result.returncode, 0, result.stderr)
        names = [r["method_name"] for r in self.pds.requests]
        self.assertEqual(names, ["app.bsky.feed.getTimeline", "com.atproto.server.refreshSession",
                                 "app.bsky.feed.getTimeline"])

    def test_a_refused_refresh_ends_the_session_but_keeps_the_handle(self):
        self.write_session(access=jwt("stale", time.time() - 10))
        self.pds.refresh_refused = True
        result = self.run_helper("timeline")
        self.assertEqual(result.returncode, helper.EXIT_SESSION)
        self.assertIn(b"session_expired", result.stderr)
        auth = self.stored()
        self.assertEqual((auth["accessJwt"], auth["refreshJwt"]), ("", ""))
        self.assertEqual(auth["handle"], "tester.example.org")
        self.assertFalse(json.loads(self.run_helper("load").stdout)["auth"]["hasSession"])

    def test_concurrent_helpers_refresh_only_once(self):
        self.write_session(access=jwt("stale", time.time() - 10))
        environment = dict(os.environ)
        environment.update(self.environ)
        procs = [subprocess.Popen([sys.executable, HELPER, "timeline"], env=environment,
                                  stdout=subprocess.PIPE, stderr=subprocess.PIPE) for _ in range(4)]
        codes = [p.wait(timeout=60) for p in procs]
        for p in procs:
            p.stdout.close()
            p.stderr.close()
        self.assertEqual(codes, [0, 0, 0, 0])
        self.assertEqual(len(self.pds.calls("com.atproto.server.refreshSession")), 1)

    def test_logout_revokes_and_forgets_everything(self):
        self.write_session()
        result = self.run_helper("logout")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(len(self.pds.calls("com.atproto.server.deleteSession")), 1)
        self.assertEqual(self.stored(), helper.empty_auth())

    def test_logout_works_offline(self):
        self.write_session()
        auth = self.stored()
        auth["pds"] = "https://127.0.0.1:1"
        helper.write_state({"auth": auth})
        result = self.run_helper("logout")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.stored(), helper.empty_auth())

    def test_a_request_without_a_session_is_refused_locally(self):
        result = self.run_helper("timeline")
        self.assertEqual(result.returncode, helper.EXIT_STATE)
        self.assertEqual(self.pds.requests, [])


# -------------------------------------------------------------- state file


class StateFile(HelperTestCase):
    def test_state_file_is_owner_only(self):
        self.login()
        self.assertEqual(stat.S_IMODE(os.stat(self.state).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(self.state)).st_mode), 0o700)

    def test_a_symlinked_state_file_is_refused_instead_of_replaced(self):
        os.makedirs(os.path.dirname(self.state), exist_ok=True)
        target = os.path.join(self.state_dir, "elsewhere")
        with open(target, "w") as handle:
            handle.write("untouched")
        os.symlink(target, self.state)
        result = self.run_helper("login", self.pds.base, "tester.example.org",
                                 env={"BLUESKY_APP_PASSWORD": PASSWORD})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"state_file_unsafe", result.stderr)
        self.assertTrue(os.path.islink(self.state))
        with open(target) as handle:
            self.assertEqual(handle.read(), "untouched")

    def test_a_symlinked_state_file_is_not_read_either(self):
        os.makedirs(os.path.dirname(self.state), exist_ok=True)
        target = os.path.join(self.state_dir, "planted.json")
        with open(target, "w") as handle:
            json.dump({"auth": {"did": DID, "accessJwt": "x", "refreshJwt": "y"}}, handle)
        os.symlink(target, self.state)
        self.assertFalse(json.loads(self.run_helper("load").stdout)["auth"]["hasSession"])

    def test_missing_and_corrupt_state_load_as_logged_out(self):
        self.assertFalse(json.loads(self.run_helper("load").stdout)["auth"]["hasSession"])
        os.makedirs(os.path.dirname(self.state), exist_ok=True)
        with open(self.state, "w") as handle:
            handle.write("{not json")
        result = self.run_helper("load")
        self.assertEqual(result.returncode, 0)
        self.assertFalse(json.loads(result.stdout)["auth"]["hasSession"])


# ------------------------------------------------------------------ records


class Records(HelperTestCase):
    URI = "at://did:plc:author1234567890/app.bsky.feed.post/3kpost"
    CID = "bafyreipost1234567"

    def test_a_like_points_at_the_post_and_lives_in_the_users_repo(self):
        self.write_session()
        result = self.run_helper("like", self.URI, self.CID)
        self.assertEqual(result.returncode, 0, result.stderr)
        sent = self.pds.calls("com.atproto.repo.createRecord")[0]["json"]
        self.assertEqual(sent["repo"], DID)
        self.assertEqual(sent["collection"], "app.bsky.feed.like")
        self.assertEqual(sent["record"]["subject"], {"uri": self.URI, "cid": self.CID})
        self.assertTrue(sent["record"]["createdAt"].endswith("Z"))
        self.assertTrue(json.loads(result.stdout)["uri"].startswith("at://" + DID))

    def test_follow_takes_a_did(self):
        self.write_session()
        result = self.run_helper("follow", OTHER_DID)
        self.assertEqual(result.returncode, 0, result.stderr)
        sent = self.pds.calls("com.atproto.repo.createRecord")[0]["json"]
        self.assertEqual(sent["record"]["subject"], OTHER_DID)

    def test_unlike_deletes_the_users_own_like(self):
        self.write_session()
        result = self.run_helper("unlike", "at://%s/app.bsky.feed.like/3klike" % DID)
        self.assertEqual(result.returncode, 0, result.stderr)
        sent = self.pds.calls("com.atproto.repo.deleteRecord")[0]["json"]
        self.assertEqual(sent, {"repo": DID, "collection": "app.bsky.feed.like", "rkey": "3klike"})

    def test_unlike_cannot_be_talked_into_deleting_something_else(self):
        self.write_session()
        for uri in ("at://%s/app.bsky.feed.post/3kpost" % DID,
                    "at://%s/app.bsky.feed.like/3klike" % OTHER_DID):
            result = self.run_helper("unlike", uri)
            self.assertNotEqual(result.returncode, 0, uri)
            self.assertIn(b"not_your_record", result.stderr)
        self.assertEqual(self.pds.calls("com.atproto.repo.deleteRecord"), [])

    def test_malformed_references_never_leave_the_machine(self):
        self.write_session()
        bad = [("like", "https://evil.example/x", self.CID),
               ("like", self.URI, "not a cid"),
               ("like", "at://did:plc:x/app.bsky.feed.post/../../y", self.CID),
               ("repost", "at://notadid/app.bsky.feed.post/3k", self.CID),
               ("follow", "alice.bsky.social"),
               ("unbookmark", "javascript:alert(1)")]
        for args in bad:
            result = self.run_helper(*args)
            self.assertEqual(result.returncode, helper.EXIT_USAGE, args)
            self.assertEqual(result.stdout, b"")
        self.assertEqual(self.pds.requests, [])

    def test_bookmarks_use_the_bookmark_api(self):
        self.write_session()
        self.assertEqual(self.run_helper("bookmark", self.URI, self.CID).returncode, 0)
        self.assertEqual(self.run_helper("unbookmark", self.URI).returncode, 0)
        self.assertEqual(self.pds.calls("app.bsky.bookmark.createBookmark")[0]["json"],
                         {"uri": self.URI, "cid": self.CID})
        self.assertEqual(self.pds.calls("app.bsky.bookmark.deleteBookmark")[0]["json"],
                         {"uri": self.URI})


# ------------------------------------------------------------------- feeds


class Feeds(HelperTestCase):
    def test_timeline_and_discover_page_with_a_cursor(self):
        self.write_session()
        self.assertEqual(self.run_helper("timeline", "c1").returncode, 0)
        self.assertEqual(self.run_helper("discover").returncode, 0)
        timeline, discover = self.pds.requests
        self.assertEqual(timeline["query"]["cursor"], ["c1"])
        self.assertEqual(timeline["query"]["limit"], [str(helper.PAGE_SIZE)])
        self.assertEqual(discover["query"]["feed"], [helper.DISCOVER_FEED])

    def test_a_cursor_with_whitespace_is_refused(self):
        self.write_session()
        self.assertEqual(self.run_helper("timeline", "a b").returncode, helper.EXIT_USAGE)

    def test_mentions_are_hydrated_in_order_and_gone_posts_are_dropped(self):
        self.write_session()
        uris = ["at://did:plc:a1234567890/app.bsky.feed.post/%d" % i for i in range(4)]
        self.pds.notifications = [
            {"uri": uris[0], "reason": "reply"},
            {"uri": uris[1], "reason": "like"},
            {"uri": uris[2], "reason": "mention"},
            {"uri": uris[3], "reason": "quote"},
        ]
        self.pds.posts = {uris[0]: {"uri": uris[0]}, uris[3]: {"uri": uris[3]}}
        result = self.run_helper("mentions")
        self.assertEqual(result.returncode, 0, result.stderr)
        page = json.loads(result.stdout)
        self.assertEqual([i["post"]["uri"] for i in page["feed"]], [uris[0], uris[3]])
        self.assertEqual([i["notificationReason"] for i in page["feed"]], ["reply", "quote"])
        self.assertEqual(page["cursor"], "n2")
        listed = self.pds.calls("app.bsky.notification.listNotifications")[0]["query"]
        self.assertEqual(sorted(listed["reasons"]), ["mention", "quote", "reply"])
        hydrated = self.pds.calls("app.bsky.feed.getPosts")[0]["query"]["uris"]
        self.assertNotIn(uris[1], hydrated, "a like is not a mention")


# -------------------------------------------------------------------- posts


BLOB = {"$type": "blob", "ref": {"$link": "bafkreiuploaded1234"}, "mimeType": "image/jpeg", "size": 1234}


def no_card(_url):
    return None


class BuildingPosts(HelperTestCase):
    def build(self, spec, resolve=lambda h: "did:plc:alice1234567890" if h == "alice.example.org" else ""):
        return helper.build_post(spec, resolve=resolve, card=no_card)

    def test_facets_use_utf8_byte_offsets(self):
        text = "Grüße @alice.example.org schau https://example.org/a_(b)). #Omarchy #1"
        record = self.build({"text": text})
        encoded = text.encode("utf-8")
        found = {}
        for facet in record["facets"]:
            piece = encoded[facet["index"]["byteStart"]:facet["index"]["byteEnd"]].decode()
            found[facet["features"][0]["$type"].rsplit("#", 1)[1]] = (piece, facet["features"][0])
        self.assertEqual(found["mention"][0], "@alice.example.org")
        self.assertEqual(found["mention"][1]["did"], "did:plc:alice1234567890")
        self.assertEqual(found["link"][0], "https://example.org/a_(b)")
        self.assertEqual(found["tag"][0], "#Omarchy")
        self.assertEqual(found["tag"][1]["tag"], "Omarchy")

    def test_an_unknown_handle_stays_plain_text(self):
        record = self.build({"text": "hi @nobody.example.org"})
        self.assertNotIn("facets", record)

    def test_the_text_is_held_to_the_record_limits(self):
        self.assertEqual(len(self.build({"text": "x" * 300})["text"]), 300)
        for text in ("x" * 301, "😀" * 300 + "x"):
            with self.assertRaises(helper.HelperError) as caught:
                self.build({"text": text})
            self.assertEqual(caught.exception.message, "post_too_long")
        with self.assertRaises(helper.HelperError):
            self.build({"text": "   "})

    def test_a_reply_carries_root_and_parent(self):
        ref = {"uri": "at://did:plc:a1234567890/app.bsky.feed.post/3k", "cid": "bafyreiroot1234"}
        record = self.build({"text": "yes", "reply": {"root": ref, "parent": ref}})
        self.assertEqual(record["reply"], {"root": ref, "parent": ref})
        with self.assertRaises(helper.HelperError):
            self.build({"text": "x", "reply": {"root": ref, "parent": {"uri": "https://evil", "cid": "x"}}})

    def test_images_carry_alt_text_and_a_clean_blob(self):
        dirty = dict(BLOB, extra="smuggled")
        record = self.build({"text": "", "images": [
            {"blob": dirty, "alt": "a cat", "aspectRatio": {"width": 4, "height": 3}}]})
        image = record["embed"]["images"][0]
        self.assertEqual(record["embed"]["$type"], "app.bsky.embed.images")
        self.assertEqual(image["alt"], "a cat")
        self.assertEqual(image["aspectRatio"], {"width": 4, "height": 3})
        self.assertNotIn("extra", image["image"])

    def test_a_forged_blob_is_refused(self):
        for blob in ({"$type": "blob", "ref": {"$link": "x y"}, "mimeType": "image/png", "size": 1},
                     dict(BLOB, mimeType="text/html"),
                     dict(BLOB, size=helper.MAX_BLOB_BYTES + 1),
                     "bafkrei"):
            with self.assertRaises(helper.HelperError, msg=str(blob)):
                self.build({"text": "x", "images": [{"blob": blob}]})
        with self.assertRaises(helper.HelperError):
            self.build({"text": "x", "images": [{"blob": BLOB}] * 5})

    def test_the_first_link_gets_a_card_when_there_are_no_images(self):
        seen = []

        def card(url):
            seen.append(url)
            return {"$type": "app.bsky.embed.external", "external": {"uri": url, "title": "t", "description": ""}}

        record = helper.build_post({"text": "see https://a.example and https://b.example"},
                                   resolve=lambda h: "", card=card)
        self.assertEqual(seen, ["https://a.example"])
        self.assertEqual(record["embed"]["$type"], "app.bsky.embed.external")
        record = helper.build_post({"text": "see https://a.example", "images": [{"blob": BLOB}]},
                                   resolve=lambda h: "", card=card)
        self.assertEqual(record["embed"]["$type"], "app.bsky.embed.images")

    def test_the_language_comes_from_the_locale(self):
        for value, expected in (("de_DE.UTF-8", ["de"]), ("C", []), ("en_US", ["en"]), ("", [])):
            with unittest.mock.patch.dict(os.environ, {"LANG": value, "LC_ALL": "", "LC_MESSAGES": ""}):
                self.assertEqual(helper.post_langs(), expected, value)

    def test_a_post_travels_on_the_environment(self):
        self.write_session()
        spec = json.dumps({"text": "hello world"})
        self.assertNotEqual(self.run_helper("post", spec).returncode, 0, "no argument accepted")
        result = self.run_helper("post", env={"BLUESKY_POST_JSON": spec, "LANG": "de_DE.UTF-8"})
        self.assertEqual(result.returncode, 0, result.stderr)
        sent = self.pds.calls("com.atproto.repo.createRecord")[-1]["json"]
        self.assertEqual(sent["collection"], "app.bsky.feed.post")
        self.assertEqual(sent["record"]["text"], "hello world")
        self.assertEqual(sent["record"]["langs"], ["de"])
        self.assertEqual(json.loads(result.stdout)["cid"], "bafyreicreated123")


# ------------------------------------------------------------------ uploads


class UploadingImages(HelperTestCase):
    def setUp(self):
        super().setUp()
        self.write_session()
        self.files = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.files, True)

    def image(self, name, payload):
        path = os.path.join(self.files, name)
        with open(path, "wb") as handle:
            handle.write(payload)
        return path

    def png(self, width=3, height=2):
        import struct
        import zlib
        row = b"\x00" + b"\xff\x00\x00" * width
        def chunk(kind, data):
            return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
        return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(row * height)) + chunk(b"IEND", b""))

    def upload(self, path, *args, env=None):
        environment = {"BLUESKY_UPLOAD_PATH": path}
        environment.update(env or {})
        return self.run_helper("upload", *args, env=environment)

    def test_the_picture_is_uploaded_as_is_without_imagemagick(self):
        payload = self.png()
        result = self.upload(self.image("a.png", payload))
        self.assertEqual(result.returncode, 0, result.stderr)
        sent = self.pds.calls("com.atproto.repo.uploadBlob")[0]
        self.assertEqual(sent["raw"], payload)
        self.assertEqual(sent["headers"]["Content-Type"], "image/png")
        answer = json.loads(result.stdout)
        self.assertEqual(answer["aspectRatio"], {"width": 3, "height": 2})
        self.assertEqual(answer["blob"]["ref"]["$link"], "bafkreiuploaded1234")

    def test_the_path_is_never_an_argument(self):
        path = self.image("a.png", self.png())
        result = self.run_helper("upload", path)
        self.assertEqual(result.returncode, helper.EXIT_USAGE)
        result = self.run_helper("upload")
        self.assertIn(b"missing_upload_path", result.stderr)
        self.assertEqual(self.pds.calls("com.atproto.repo.uploadBlob"), [])

    def test_an_oversized_picture_is_refused_without_imagemagick(self):
        path = self.image("big.jpg", b"\xff\xd8" + b"\0" * (helper.MAX_BLOB_BYTES + 10))
        result = self.upload(path)
        self.assertIn(b"image_too_large", result.stderr)
        self.assertEqual(self.pds.calls("com.atproto.repo.uploadBlob"), [])

    def test_what_is_not_an_image_file_is_refused_before_it_is_sent(self):
        fifo = os.path.join(self.files, "pipe.png")
        os.mkfifo(fifo)
        cases = [(fifo, b"not_a_regular_file"),
                 (self.files, b"not_a_regular_file"),
                 (self.image("doc.pdf", b"%PDF"), b"unsupported_image"),
                 (os.path.join(self.files, "missing.png"), b"unreadable_file")]
        for path, error in cases:
            result = self.upload(path)
            self.assertIn(error, result.stderr, path)
            self.assertEqual(result.stdout, b"")
        self.assertEqual(self.pds.calls("com.atproto.repo.uploadBlob"), [])

    @unittest.skipUnless(shutil.which("magick"), "ImageMagick is not installed")
    def test_imagemagick_shrinks_a_large_photo_and_strips_its_metadata(self):
        path = os.path.join(self.files, "photo.jpg")
        subprocess.run(["magick", "-size", "3000x2400", "plasma:", "+noise", "Random",
                        "-set", "comment", "secret-location", "-quality", "100", path],
                       check=True, capture_output=True)
        self.assertGreater(os.path.getsize(path), helper.MAX_BLOB_BYTES)
        result = self.upload(path, env={"BLUESKY_IMAGE_TOOL": shutil.which("magick")})
        self.assertEqual(result.returncode, 0, result.stderr)
        sent = self.pds.calls("com.atproto.repo.uploadBlob")[0]["raw"]
        self.assertLessEqual(len(sent), helper.MAX_BLOB_BYTES)
        self.assertNotIn(b"secret-location", sent)
        ratio = json.loads(result.stdout)["aspectRatio"]
        self.assertEqual(max(ratio["width"], ratio["height"]), helper.MAX_IMAGE_SIDE)


class Counting(unittest.TestCase):
    def test_graphemes(self):
        cases = {"": 0, "hello": 5, "é": 1, "👍🏽": 1, "🇩🇪🇫🇷": 2, "👨‍👩‍👧": 1, "❤️": 1}
        for text, expected in cases.items():
            self.assertEqual(helper.grapheme_count(text), expected, repr(text))

    def test_jpeg_and_gif_sizes(self):
        jpeg = (b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
                b"\xff\xc0\x00\x11\x08\x01\xe0\x02\x80\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01")
        self.assertEqual(helper.image_size(jpeg), (640, 480))
        self.assertEqual(helper.image_size(b"GIF89a\x0a\x00\x05\x00"), (10, 5))
        self.assertIsNone(helper.image_size(b"garbage"))


import unittest.mock  # noqa: E402  (used by test_the_language_comes_from_the_locale)


if __name__ == "__main__":
    unittest.main()
