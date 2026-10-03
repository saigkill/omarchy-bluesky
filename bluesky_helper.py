#!/usr/bin/env python3
"""HTTP client for the Omarchy Bluesky panel.

Credentials are owned by this process and nothing else. The panel never sees
the app password after handing it over once, and never sees the session tokens
at all:

- The app password arrives through BLUESKY_APP_PASSWORD on the environment,
  because /proc/<pid>/cmdline is world readable while /proc/<pid>/environ is
  readable by the owner alone. It is exchanged for a session right away and is
  not written anywhere.
- The access and refresh tokens live in a 0600 state file that only this script
  reads and writes. The panel's view of it is {service, handle, did,
  hasSession}.
- Every request goes to the PDS recorded at login, over TLS, and a redirect
  that changes origin is refused, so a token can only ever travel to the server
  that issued it.

The panel calls one subcommand per action (timeline, like, post, ...) rather
than a generic "fetch this URL", so the set of things a caller can make this
script do with a token is exactly the set of subcommands below.

Structured input that is not secret but does not fit argv nicely (a post with
its reply reference and images) and the path of a local image travel on the
environment as well: BLUESKY_POST_JSON and BLUESKY_UPLOAD_PATH. A filename says
what is about to be published, so it stays out of ps output too.
"""

import base64
import datetime
import fcntl
import html as html_module
import http.client
import ipaddress
import json
import os
import re
import shutil
import socket
import ssl
import stat
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request

# Keep in sync with Model.js; tests/test_model.js asserts that they match.
APP_NAME = "Omarchy Bluesky"
DEFAULT_SERVICE = "https://bsky.social"
DISCOVER_FEED = "at://did:plc:z72i7hdynmk6r22z27h6tvur/app.bsky.feed.generator/whats-hot"
PAGE_SIZE = 40
# getPosts takes at most 25 URIs, so a page of mentions is sized to fit one
# hydration request instead of two.
MENTIONS_PAGE_SIZE = 25
MENTION_REASONS = ("mention", "reply", "quote")

MAX_POST_GRAPHEMES = 300
MAX_POST_BYTES = 3000
MAX_IMAGES = 4

TIMEOUT = 30
# A link preview is fetched while the user waits for Post to come back, so it
# gets a much shorter leash than an API call. A slow site costs the card, not
# the post.
CARD_TIMEOUT = 8
# A preview fetch follows at most this many redirects, each one re-checked.
MAX_CARD_REDIRECTS = 5

# A timeline page is a few hundred kilobytes at most. 10 MiB is generous
# headroom while still being far too small for a misbehaving server to exhaust
# the helper (and, downstream, the StdioCollector that buffers its stdout).
MAX_RESPONSE_BYTES = 10 * 1024 * 1024
# Only the <head> of a linked page is needed for its OpenGraph tags.
MAX_PAGE_BYTES = 1024 * 1024

# app.bsky.embed.images accepts blobs of up to 2,000,000 bytes (formerly 1 MB).
MAX_BLOB_BYTES = 2000000
# What is read from disk before any processing. A phone photo is 3-12 MB; the
# re-encode below brings it under the blob limit. Anything above this is not a
# picture worth reading into memory.
MAX_SOURCE_BYTES = 40 * 1024 * 1024
# The longest side after re-encoding. Bluesky's own client scales to 2000 px.
MAX_IMAGE_SIDE = 2000

IMAGE_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}
# ImageMagick coder names, so the input format is named rather than guessed
# from file contents (which is how ImageMagick ends up picking a delegate).
MAGICK_CODERS = {
    "image/jpeg": "jpeg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
}

EXIT_USAGE = 2
EXIT_INSECURE = 3
EXIT_STATE = 4
EXIT_HTTP = 5
EXIT_NETWORK = 6
# The refresh token itself was refused: the user has to log in again. The panel
# tells this apart from a transient failure by the exit status.
EXIT_SESSION = 7

DID_RE = re.compile(r"^did:[a-z]+:[A-Za-z0-9._:%-]{1,2048}$")
CID_RE = re.compile(r"^[A-Za-z0-9]{8,128}$")
RKEY_RE = re.compile(r"^[A-Za-z0-9._:~-]{1,512}$")
NSID_RE = re.compile(r"^[a-z][a-z0-9-]*(\.[a-z0-9-]+)+\.[a-zA-Z][a-zA-Z0-9]*$")
HANDLE_RE = re.compile(
    r"^([a-zA-Z0-9]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?\.)+[a-zA-Z]([a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$")


class HelperError(Exception):
    def __init__(self, message, status):
        super().__init__(message)
        self.message = message
        self.status = status


# ---------------------------------------------------------------- state file


def auth_file():
    override = os.environ.get("BLUESKY_AUTH_FILE", "").strip()
    if override:
        return override
    base = os.environ.get("XDG_STATE_HOME", "").strip()
    if not base:
        base = os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "omarchy-bluesky", "auth.json")


def empty_auth():
    return {"service": "", "pds": "", "handle": "", "did": "",
            "accessJwt": "", "refreshJwt": ""}


def read_state():
    path = auth_file()
    try:
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
    except OSError:
        return {"auth": empty_auth()}
    try:
        with os.fdopen(descriptor, "r", encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        return {"auth": empty_auth()}
    if not isinstance(data, dict) or not isinstance(data.get("auth"), dict):
        return {"auth": empty_auth()}
    auth = empty_auth()
    for key, value in data["auth"].items():
        if key in auth:
            auth[key] = "" if value is None else str(value)
    return {"auth": auth}


def write_state(state):
    """Write the state file atomically, owner-only, never through a symlink.

    Two helpers can run at once (the panel loads three feeds in a row while an
    action is in flight), so a reader must never see a half-written file: the
    new content goes to a temporary file in the same directory and replaces the
    old one in one rename. A symlink planted at the path is refused outright
    rather than replaced, so a planted link is noticed instead of silently
    papered over. The temporary file is created 0600 by mkstemp and the mode is
    set again, since a umask can only ever remove bits.
    """
    path = auth_file()
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, mode=0o700, exist_ok=True)
    try:
        if stat.S_ISLNK(os.lstat(path).st_mode):
            raise HelperError("state_file_unsafe", EXIT_STATE)
    except FileNotFoundError:
        pass
    payload = json.dumps({"auth": state["auth"]})
    descriptor, temporary = tempfile.mkstemp(prefix=".auth-", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            os.fchmod(handle.fileno(), 0o600)
            handle.write(payload)
        os.replace(temporary, path)
    except OSError as error:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise HelperError("state_file_unsafe", EXIT_STATE) from error


class StateLock:
    """Serialises session refreshes between concurrently running helpers.

    A refresh token is rotated on use. Two helpers that both notice an expired
    access token and both refresh would race, and the loser would hold a
    refresh token the server has already replaced. Under the lock, the second
    one re-reads the state file and finds the fresh session the first one
    wrote.
    """

    def __enter__(self):
        path = auth_file() + ".lock"
        os.makedirs(os.path.dirname(path) or ".", mode=0o700, exist_ok=True)
        self.descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        fcntl.flock(self.descriptor, fcntl.LOCK_EX)
        return self

    def __exit__(self, *exc):
        fcntl.flock(self.descriptor, fcntl.LOCK_UN)
        os.close(self.descriptor)
        return False


def public_state(state):
    """The view of the state file that is safe to hand back to the panel."""
    auth = state["auth"]
    return {
        "auth": {
            "service": auth["service"],
            "handle": auth["handle"],
            "did": auth["did"],
            "hasSession": bool(auth["accessJwt"] and auth["refreshJwt"]),
        }
    }


# ------------------------------------------------------------------- URLs


def is_loopback(host):
    """True only for a name that really is this machine.

    A prefix test such as host.startswith("127.") is not enough: the name
    "127.0.0.1.evil.example" resolves through DNS, so the address has to be
    parsed as an address.
    """
    name = (host or "").strip("[]").rstrip(".").lower()
    if not name:
        return False
    if name == "localhost":
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        return False


def secure_base(service):
    """Validate a server URL and refuse anything a token must not go to.

    https only, with plain http allowed for loopback alone (a PDS under
    development). Userinfo is refused because "bsky.social@evil.example" is a
    host that looks like the right one and is not. Paths, queries and
    fragments are dropped: XRPC lives at /xrpc on the origin.
    """
    raw = (service or "").strip()
    if not raw:
        raise HelperError("missing_service", EXIT_USAGE)
    parts = urllib.parse.urlsplit(raw)
    if parts.username is not None or parts.password is not None or "@" in parts.netloc:
        raise HelperError("insecure_service", EXIT_INSECURE)
    if not parts.hostname:
        raise HelperError("insecure_service", EXIT_INSECURE)
    if parts.scheme == "https" or (parts.scheme == "http" and is_loopback(parts.hostname)):
        return parts.scheme + "://" + parts.netloc.lower()
    raise HelperError("insecure_service", EXIT_INSECURE)


def normalize_service(raw):
    text = (raw or "").strip()
    if not text:
        return DEFAULT_SERVICE
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", text):
        # "host:8443" is a host and a port; "javascript:..." is a scheme.
        if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:(?!\d)", text):
            raise HelperError("insecure_service", EXIT_INSECURE)
        text = "https://" + text
    return secure_base(text)


def check_nsid(nsid):
    if not NSID_RE.match(nsid or ""):
        raise HelperError("bad_method", EXIT_USAGE)
    return nsid


def read_capped(response, limit=MAX_RESPONSE_BYTES):
    """Read a body, refusing anything larger than `limit`.

    Reading one byte past the limit is enough to tell "exactly at the limit"
    from "over it" without buffering more than that.
    """
    payload = response.read(limit + 1)
    if len(payload) > limit:
        raise HelperError("response_too_large", EXIT_HTTP)
    return payload


class SameOriginRedirect(urllib.request.HTTPRedirectHandler):
    """Never let a redirect carry the Authorization header to another origin."""

    max_redirections = 3

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        current = urllib.parse.urlsplit(req.full_url)
        target = urllib.parse.urlsplit(newurl)
        if (current.scheme, current.netloc) != (target.scheme, target.netloc):
            raise HelperError("cross_origin_redirect", EXIT_HTTP)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


# ------------------------------------------------------------------ requests


class XrpcError(Exception):
    """An XRPC error answer, with the server's machine-readable error name."""

    def __init__(self, status, name):
        super().__init__(name)
        self.status = status
        self.name = name


def send(request):
    opener = urllib.request.build_opener(SameOriginRedirect)
    try:
        with opener.open(request, timeout=TIMEOUT) as response:
            return read_capped(response)
    except urllib.error.HTTPError as error:
        name = ""
        try:
            body = json.loads(error.read(64 * 1024).decode("utf-8", "replace"))
            if isinstance(body, dict):
                name = str(body.get("error") or "")
        except (OSError, ValueError):
            pass
        raise XrpcError(error.code, name) from None
    except (urllib.error.URLError, OSError):
        raise HelperError("network_error", EXIT_NETWORK) from None


def xrpc_url(base, nsid, params=None):
    url = secure_base(base) + "/xrpc/" + check_nsid(nsid)
    if params:
        url += "?" + urllib.parse.urlencode(params, doseq=True)
    return url


def raw_call(base, method, nsid, token=None, params=None, body=None, content_type=None):
    headers = {"Accept": "application/json", "User-Agent": APP_NAME}
    if token:
        headers["Authorization"] = "Bearer " + token
    data = None
    if body is not None:
        if isinstance(body, (bytes, bytearray)):
            data = bytes(body)
            headers["Content-Type"] = content_type or "application/octet-stream"
        else:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        xrpc_url(base, nsid, params), data=data, headers=headers, method=method)
    payload = send(request)
    if not payload:
        return {}
    return json.loads(payload.decode("utf-8", "replace"))


def jwt_expiry(token):
    """The exp claim of a JWT, or 0. Read only to schedule a refresh: the
    server verifies the signature, so nothing here depends on trusting it."""
    try:
        segment = token.split(".")[1]
        segment += "=" * (-len(segment) % 4)
        claims = json.loads(base64.urlsafe_b64decode(segment.encode("ascii")))
        return int(claims.get("exp") or 0)
    except (IndexError, ValueError, TypeError, AttributeError):
        return 0


def pds_from_did_doc(doc):
    """The user's PDS from the DID document createSession answers with.

    Logging in goes through the entryway (bsky.social), but the account lives on
    a PDS, and that is where requests belong. Only an https endpoint is taken;
    anything else falls back to the server the user logged in to.
    """
    if not isinstance(doc, dict):
        return ""
    services = doc.get("service")
    if not isinstance(services, list):
        return ""
    for entry in services:
        if not isinstance(entry, dict):
            continue
        if entry.get("id") in ("#atproto_pds", doc.get("id", "") + "#atproto_pds") \
                and entry.get("type") == "AtprotoPersonalDataServer":
            try:
                return secure_base(str(entry.get("serviceEndpoint") or ""))
            except HelperError:
                return ""
    return ""


def store_session(state, session):
    access = str(session.get("accessJwt") or "")
    refresh = str(session.get("refreshJwt") or "")
    did = str(session.get("did") or "")
    if not access or not refresh or not DID_RE.match(did):
        raise HelperError("login_failed", EXIT_HTTP)
    auth = state["auth"]
    auth["accessJwt"] = access
    auth["refreshJwt"] = refresh
    auth["did"] = did
    auth["handle"] = str(session.get("handle") or auth["handle"])
    pds = pds_from_did_doc(session.get("didDoc"))
    if pds:
        auth["pds"] = pds
    elif not auth["pds"]:
        auth["pds"] = auth["service"]


def end_session(state):
    """Drop the tokens but keep who the user was, so the login form can be
    prefilled after an expired session."""
    state["auth"]["accessJwt"] = ""
    state["auth"]["refreshJwt"] = ""
    write_state(state)


def refresh_session(stale_access):
    """Exchange the refresh token for a new pair, at most once across helpers.

    Returns the fresh state. If another helper refreshed while this one waited
    for the lock, its result is used as is.
    """
    with StateLock():
        state = read_state()
        auth = state["auth"]
        if not auth["refreshJwt"]:
            raise HelperError("session_expired", EXIT_SESSION)
        if auth["accessJwt"] and auth["accessJwt"] != stale_access:
            return state
        try:
            session = raw_call(auth["pds"] or auth["service"], "POST",
                               "com.atproto.server.refreshSession", token=auth["refreshJwt"])
        except XrpcError as error:
            if error.status in (400, 401):
                end_session(state)
                raise HelperError("session_expired", EXIT_SESSION) from None
            raise HelperError("http_%d" % error.status, EXIT_HTTP) from None
        store_session(state, session)
        write_state(state)
        return state


def session_state():
    state = read_state()
    auth = state["auth"]
    if not auth["accessJwt"] or not auth["refreshJwt"] or not auth["did"]:
        raise HelperError("not_authenticated", EXIT_STATE)
    # Refreshing a minute early saves the round trip that would otherwise be
    # spent on a request that is certain to come back ExpiredToken.
    expiry = jwt_expiry(auth["accessJwt"])
    if expiry and expiry - 60 < time.time():
        state = refresh_session(auth["accessJwt"])
    return state


def call(method, nsid, params=None, body=None, content_type=None):
    """An authenticated XRPC call on the user's PDS, refreshing once on expiry."""
    state = session_state()
    for attempt in (0, 1):
        auth = state["auth"]
        try:
            return raw_call(auth["pds"] or auth["service"], method, nsid,
                            token=auth["accessJwt"], params=params, body=body,
                            content_type=content_type)
        except XrpcError as error:
            expired = error.name in ("ExpiredToken", "InvalidToken") or error.status == 401
            if attempt == 0 and expired:
                state = refresh_session(auth["accessJwt"])
                continue
            raise HelperError("http_%d%s" % (error.status, ":" + error.name if error.name else ""),
                              EXIT_HTTP) from None
    raise HelperError("session_expired", EXIT_SESSION)


# ------------------------------------------------------------ record helpers


def check_did(value):
    text = str(value or "")
    if not DID_RE.match(text):
        raise HelperError("bad_did", EXIT_USAGE)
    return text


def check_cid(value):
    text = str(value or "")
    if not CID_RE.match(text):
        raise HelperError("bad_cid", EXIT_USAGE)
    return text


def parse_at_uri(value):
    """Split at://<did>/<collection>/<rkey>, refusing anything else."""
    text = str(value or "")
    match = re.match(r"^at://([^/]+)/([^/]+)/([^/?#]+)$", text)
    if not match:
        raise HelperError("bad_uri", EXIT_USAGE)
    did, collection, rkey = match.groups()
    check_did(did)
    check_nsid(collection)
    if not RKEY_RE.match(rkey):
        raise HelperError("bad_uri", EXIT_USAGE)
    return did, collection, rkey


def strong_ref(uri, cid):
    parse_at_uri(uri)
    return {"uri": str(uri), "cid": check_cid(cid)}


def now_iso():
    now = datetime.datetime.now(datetime.timezone.utc)
    return now.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def create_record(collection, record):
    state = session_state()
    return call("POST", "com.atproto.repo.createRecord", body={
        "repo": state["auth"]["did"],
        "collection": collection,
        "record": record,
    })


def delete_own_record(uri, collection):
    """Delete a record the user owns, of exactly the expected kind.

    The URI comes from the panel (it is the viewer.like of a post, say), so it is
    checked against the session's own DID and the collection the subcommand is
    about: "unlike" must not be talked into deleting a post.
    """
    did, found_collection, rkey = parse_at_uri(uri)
    state = session_state()
    if did != state["auth"]["did"] or found_collection != collection:
        raise HelperError("not_your_record", EXIT_USAGE)
    call("POST", "com.atproto.repo.deleteRecord", body={
        "repo": did, "collection": collection, "rkey": rkey,
    })
    return {"ok": True}


# -------------------------------------------------------------------- facets

URL_RE = re.compile(r"https?://[^\s<>\"]+", re.IGNORECASE)
MENTION_RE = re.compile(r"(?:^|(?<=[\s(]))@([a-zA-Z0-9][a-zA-Z0-9.-]*\.[a-zA-Z][a-zA-Z0-9-]*)")
TAG_RE = re.compile(r"(?:^|(?<=\s))[#＃]([^\s#＃]+)")
TRAILING_PUNCTUATION = ".,;:!?\"'”’)]}>"


def byte_span(text, start, end):
    return {"byteStart": len(text[:start].encode("utf-8")),
            "byteEnd": len(text[:end].encode("utf-8"))}


def trim_trailing(value):
    """Strip punctuation that ends a sentence rather than the token. A closing
    parenthesis is kept when the token opened one itself (Wikipedia links)."""
    while value and value[-1] in TRAILING_PUNCTUATION:
        if value[-1] == ")" and value.count("(") >= value.count(")"):
            break
        value = value[:-1]
    return value


def detect_facets(text, resolve_handle):
    """Links, mentions and hashtags, with UTF-8 byte offsets as the spec wants.

    Mentions are only linked when the handle resolves to a DID: a facet with a
    made-up DID would point at nobody, so an unknown handle stays plain text.
    """
    facets = []
    taken = []

    def free(start, end):
        return all(end <= a or start >= b for a, b in taken)

    for match in URL_RE.finditer(text):
        url = trim_trailing(match.group(0))
        if len(url) <= len("https://"):
            continue
        start, end = match.start(), match.start() + len(url)
        taken.append((start, end))
        facets.append({"index": byte_span(text, start, end),
                       "features": [{"$type": "app.bsky.richtext.facet#link", "uri": url}]})

    for match in MENTION_RE.finditer(text):
        handle = trim_trailing(match.group(1)).rstrip(".").lower()
        if not HANDLE_RE.match(handle):
            continue
        start = match.start(1) - 1
        end = match.start(1) + len(handle)
        if not free(start, end):
            continue
        did = resolve_handle(handle)
        if not did:
            continue
        taken.append((start, end))
        facets.append({"index": byte_span(text, start, end),
                       "features": [{"$type": "app.bsky.richtext.facet#mention", "did": did}]})

    for match in TAG_RE.finditer(text):
        tag = trim_trailing(match.group(1))
        # A bare number is not a tag ("#1"), and the spec caps a tag at 64
        # graphemes; code points are the conservative stand-in.
        if not tag or tag.isdigit() or len(tag) > 64:
            continue
        start = match.start(1) - 1
        end = match.start(1) + len(tag)
        if not free(start, end):
            continue
        taken.append((start, end))
        facets.append({"index": byte_span(text, start, end),
                       "features": [{"$type": "app.bsky.richtext.facet#tag", "tag": tag}]})

    facets.sort(key=lambda facet: facet["index"]["byteStart"])
    return facets


def resolve_handle(handle):
    try:
        data = call("GET", "com.atproto.identity.resolveHandle", params={"handle": handle})
    except HelperError as error:
        if error.status == EXIT_SESSION:
            raise
        return ""
    did = str(data.get("did") or "") if isinstance(data, dict) else ""
    return did if DID_RE.match(did) else ""


# ------------------------------------------------------- grapheme counting
#
# The server counts graphemes. Python has no grapheme segmenter in the standard
# library, so this counts code points and lets the ones that only ever extend
# the previous character ride along: combining marks, variation selectors, skin
# tone modifiers, tag characters, and whatever follows a zero width joiner. Two
# regional indicators make one flag. The approximation errs towards counting
# more, so a post it accepts is one the server accepts. Model.js counts the
# same way.


def is_extender(code):
    return (0x0300 <= code <= 0x036F or 0x1AB0 <= code <= 0x1AFF or
            0x1DC0 <= code <= 0x1DFF or 0x20D0 <= code <= 0x20FF or
            0xFE20 <= code <= 0xFE2F or 0xFE00 <= code <= 0xFE0F or
            0x1F3FB <= code <= 0x1F3FF or 0xE0020 <= code <= 0xE007F or
            0xE0100 <= code <= 0xE01EF or code == 0x200D)


def grapheme_count(text):
    count = 0
    joined = False
    pending_flag = False
    for char in text:
        code = ord(char)
        if code == 0x200D:
            joined = True
            continue
        if joined:
            joined = False
            continue
        if is_extender(code):
            continue
        if 0x1F1E6 <= code <= 0x1F1FF:
            if pending_flag:
                pending_flag = False
                continue
            pending_flag = True
        else:
            pending_flag = False
        count += 1
    return count


# -------------------------------------------------------------------- images


def image_tool():
    """ImageMagick, if installed. BLUESKY_IMAGE_TOOL overrides the lookup, which
    is how the tests run both with and without it."""
    override = os.environ.get("BLUESKY_IMAGE_TOOL")
    if override is not None:
        return override if override and os.access(override, os.X_OK) else ""
    return shutil.which("magick") or ""


def read_local_image(raw_path):
    """Read a local image, refusing anything that is not a plain file.

    stat() decides before the file is opened: opening a fifo would block until a
    writer shows up, and a device node is not an image. The bytes actually read
    are measured too, which also catches a file that grew in between.
    """
    path = os.path.realpath(os.path.expanduser(str(raw_path or "")))
    try:
        info = os.stat(path)
    except OSError:
        raise HelperError("unreadable_file", EXIT_USAGE) from None
    if not stat.S_ISREG(info.st_mode):
        raise HelperError("not_a_regular_file", EXIT_USAGE)
    suffix = os.path.splitext(path)[1].lower()
    if suffix not in IMAGE_TYPES:
        raise HelperError("unsupported_image", EXIT_USAGE)
    if info.st_size > MAX_SOURCE_BYTES:
        raise HelperError("file_too_large", EXIT_USAGE)
    try:
        with open(path, "rb") as handle:
            payload = handle.read(MAX_SOURCE_BYTES + 1)
    except OSError:
        raise HelperError("unreadable_file", EXIT_USAGE) from None
    if len(payload) > MAX_SOURCE_BYTES:
        raise HelperError("file_too_large", EXIT_USAGE)
    return payload, IMAGE_TYPES[suffix]


def reencode(tool, payload, mime, out_format, quality):
    """One ImageMagick pass over the bytes, on stdin and stdout.

    No shell, no file name: the input coder is named from the extension that was
    already checked, so ImageMagick never guesses a format, and resource limits
    bound what a hostile file can make it allocate. -strip drops EXIF, which is
    where a phone puts the GPS position the photo was taken at.
    """
    command = [
        tool, "-limit", "memory", "256MiB", "-limit", "map", "512MiB",
        "-limit", "disk", "0", "-limit", "time", "30",
        MAGICK_CODERS[mime] + ":-[0]",
        "-auto-orient", "-strip",
        "-resize", "%dx%d>" % (MAX_IMAGE_SIDE, MAX_IMAGE_SIDE),
    ]
    if out_format == "jpeg":
        command += ["-background", "white", "-alpha", "remove", "-quality", str(quality)]
    command.append(out_format + ":-")
    try:
        result = subprocess.run(command, input=payload, capture_output=True, timeout=60)
    except (OSError, subprocess.TimeoutExpired):
        return b""
    return result.stdout if result.returncode == 0 else b""


def prepare_image(payload, mime):
    """Bring an image under the blob limit, without metadata where possible.

    With ImageMagick: every image is re-encoded once, which strips EXIF and
    scales it to MAX_IMAGE_SIDE. PNG stays PNG (screenshots compress badly as
    JPEG) unless it is still too big, everything else becomes JPEG, and the
    quality steps down until the result fits.

    Without it: the original is uploaded if it fits and refused if not.
    """
    tool = image_tool()
    if not tool:
        if len(payload) > MAX_BLOB_BYTES:
            raise HelperError("image_too_large", EXIT_USAGE)
        return payload, mime
    if mime == "image/png":
        out = reencode(tool, payload, mime, "png", 0)
        if out and len(out) <= MAX_BLOB_BYTES:
            return out, "image/png"
    for quality in (85, 75, 62, 50):
        out = reencode(tool, payload, mime, "jpeg", quality)
        if out and len(out) <= MAX_BLOB_BYTES:
            return out, "image/jpeg"
    if len(payload) <= MAX_BLOB_BYTES:
        return payload, mime
    raise HelperError("image_too_large", EXIT_USAGE)


def image_size(payload):
    """Width and height from the header of a PNG, JPEG or GIF, or None.

    The aspect ratio lets clients reserve the right space before the image has
    loaded; it is optional in the record, so an unreadable header just leaves
    it out.
    """
    try:
        if payload[:8] == b"\x89PNG\r\n\x1a\n" and payload[12:16] == b"IHDR":
            return int.from_bytes(payload[16:20], "big"), int.from_bytes(payload[20:24], "big")
        if payload[:6] in (b"GIF87a", b"GIF89a"):
            return int.from_bytes(payload[6:8], "little"), int.from_bytes(payload[8:10], "little")
        if payload[:2] == b"\xff\xd8":
            index = 2
            while index + 9 < len(payload):
                if payload[index] != 0xFF:
                    index += 1
                    continue
                marker = payload[index + 1]
                if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                    index += 2
                    continue
                length = int.from_bytes(payload[index + 2:index + 4], "big")
                if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
                    height = int.from_bytes(payload[index + 5:index + 7], "big")
                    width = int.from_bytes(payload[index + 7:index + 9], "big")
                    return width, height
                index += 2 + length
    except (IndexError, ValueError):
        return None
    return None


def check_blob(blob):
    """A blob reference as uploadBlob returned it, and nothing more.

    The panel hands it back at post time, so it is rebuilt field by field from
    what a blob ref can contain rather than passed through.
    """
    if not isinstance(blob, dict) or blob.get("$type") != "blob":
        raise HelperError("bad_blob", EXIT_USAGE)
    ref = blob.get("ref")
    if not isinstance(ref, dict):
        raise HelperError("bad_blob", EXIT_USAGE)
    mime = str(blob.get("mimeType") or "")
    if not mime.startswith("image/"):
        raise HelperError("bad_blob", EXIT_USAGE)
    size = blob.get("size")
    if not isinstance(size, int) or size < 1 or size > MAX_BLOB_BYTES:
        raise HelperError("bad_blob", EXIT_USAGE)
    return {"$type": "blob", "ref": {"$link": check_cid(ref.get("$link"))},
            "mimeType": mime, "size": size}


def check_aspect(value):
    if not isinstance(value, dict):
        return None
    width, height = value.get("width"), value.get("height")
    if isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0:
        return {"width": width, "height": height}
    return None


# ---------------------------------------------------------------- link cards


def meta_content(html, key):
    """<meta property|name="key" content="..."> in either attribute order."""
    for pattern in (
            r"<meta[^>]+(?:property|name)\s*=\s*[\"']%s[\"'][^>]*content\s*=\s*(\"[^\"]*\"|'[^']*')",
            r"<meta[^>]+content\s*=\s*(\"[^\"]*\"|'[^']*')[^>]*(?:property|name)\s*=\s*[\"']%s[\"']"):
        match = re.search(pattern % re.escape(key), html, re.IGNORECASE)
        if match:
            return unescape_html(match.group(1)[1:-1]).strip()
    return ""


def unescape_html(text):
    return html_module.unescape(text)


def is_public_address(address):
    """True only for an address on the public internet.

    The page behind a link decides where its og:image points, and the image
    ends up in the published post. Without this check a hostile page could
    point at the router, a LAN camera or a service on this machine and have
    the user publish what it returns. IPv4 carried inside IPv6 (mapped, 6to4,
    Teredo) is judged by the IPv4 address it carries.
    """
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    if ip.version == 6:
        embedded = ip.ipv4_mapped or ip.sixtofour or (ip.teredo[1] if ip.teredo else None)
        if embedded is not None:
            ip = embedded
    return ip.is_global and not ip.is_multicast


def public_addresses(host, port):
    """The addresses for host, or [] if any of them is not public.

    Every address has to pass, not just one: a name with one public and one
    private address would otherwise reach the private one on a retry.
    """
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (OSError, UnicodeError):
        return []
    addresses = []
    for info in infos:
        address = info[4][0]
        if not is_public_address(address):
            return []
        if address not in addresses:
            addresses.append(address)
    return addresses


class PinnedConnection:
    """Connect to an address that was already checked, not to the name again.

    Resolving twice would let a name answer with a public address for the
    check and a private one for the connection (DNS rebinding). The name still
    goes into the Host header and, for https, into SNI and the certificate
    check.
    """

    def __init__(self, addresses, *args, **kwargs):
        self.addresses = addresses
        super().__init__(*args, **kwargs)

    def open_socket(self):
        error = OSError("no address")
        for address in self.addresses:
            try:
                return socket.create_connection((address, self.port), self.timeout)
            except OSError as caught:
                error = caught
        raise error


class PinnedHTTPConnection(PinnedConnection, http.client.HTTPConnection):
    def connect(self):
        self.sock = self.open_socket()


class PinnedHTTPSConnection(PinnedConnection, http.client.HTTPSConnection):
    def __init__(self, addresses, host, port, timeout):
        self.tls = ssl.create_default_context()
        super().__init__(addresses, host, port, timeout=timeout, context=self.tls)

    def connect(self):
        self.sock = self.tls.wrap_socket(self.open_socket(), server_hostname=self.host)


def fetch_public(url, limit, accept):
    """GET a public URL without any credential, bounded in time and bytes.

    Only destinations on the public internet are contacted, and redirects are
    followed by hand so every hop is checked the same way. urllib is not used
    because it follows redirects on its own and honours proxy variables, and
    a proxy would resolve the name itself, past the check.
    """
    for _hop in range(MAX_CARD_REDIRECTS + 1):
        parts = urllib.parse.urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            return b"", ""
        try:
            port = parts.port or (443 if parts.scheme == "https" else 80)
        except ValueError:
            return b"", ""
        addresses = public_addresses(parts.hostname, port)
        if not addresses:
            return b"", ""
        target = urllib.parse.quote(parts.path or "/", safe="/%:@!$&'()*+,;=-._~")
        if parts.query:
            target += "?" + urllib.parse.quote(parts.query, safe="/%:@!$&'()*+,;=-._~?")
        kind = PinnedHTTPSConnection if parts.scheme == "https" else PinnedHTTPConnection
        connection = kind(addresses, parts.hostname, port, timeout=CARD_TIMEOUT)
        try:
            connection.request("GET", target, headers={
                "User-Agent": "Mozilla/5.0 (compatible; %s)" % APP_NAME, "Accept": accept})
            response = connection.getresponse()
            if response.status in (301, 302, 303, 307, 308):
                location = response.getheader("Location") or ""
                if not location:
                    return b"", ""
                url = urllib.parse.urljoin(url, location)
                continue
            if response.status != 200:
                return b"", ""
            return response.read(limit + 1), response.headers.get_content_type()
        except (http.client.HTTPException, OSError, ValueError, UnicodeError):
            return b"", ""
        finally:
            connection.close()
    return b"", ""


def link_card(url):
    """An app.bsky.embed.external for the first link of a post, or None.

    Bluesky does not build link previews on the server; the client that writes
    the post does. This one reads the page's OpenGraph tags itself, which means
    the linked site sees one request from this machine. The token is never part
    of it. A page that does not answer in time, or has no title, simply gets no
    card.
    """
    body, kind = fetch_public(url, MAX_PAGE_BYTES, "text/html,application/xhtml+xml")
    # A page longer than the cap is fine: the tags are in the <head>, and the
    # read simply stopped after it.
    if not body or kind not in ("text/html", "application/xhtml+xml"):
        return None
    html = body[:MAX_PAGE_BYTES].decode("utf-8", "replace")
    title = meta_content(html, "og:title") or meta_content(html, "twitter:title")
    if not title:
        match = re.search(r"<title[^>]*>([^<]{1,500})</title>", html, re.IGNORECASE)
        title = unescape_html(match.group(1)).strip() if match else ""
    if not title:
        return None
    description = meta_content(html, "og:description") or meta_content(html, "description")
    external = {"uri": url, "title": title[:300], "description": description[:1000]}
    image = meta_content(html, "og:image") or meta_content(html, "twitter:image")
    if image:
        image = urllib.parse.urljoin(url, image)
        thumb = card_thumb(image)
        if thumb:
            external["thumb"] = thumb
    return {"$type": "app.bsky.embed.external", "external": external}


def card_thumb(url):
    payload, kind = fetch_public(url, MAX_SOURCE_BYTES, "image/*")
    if not payload or len(payload) > MAX_SOURCE_BYTES:
        return None
    if kind not in MAGICK_CODERS:
        return None
    try:
        data, mime = prepare_image(payload, kind)
        return check_blob(upload_blob(data, mime))
    except HelperError as error:
        if error.status == EXIT_SESSION:
            raise
        return None


def upload_blob(data, mime):
    answer = call("POST", "com.atproto.repo.uploadBlob", body=data, content_type=mime)
    blob = answer.get("blob") if isinstance(answer, dict) else None
    if not isinstance(blob, dict):
        raise HelperError("upload_failed", EXIT_HTTP)
    return blob


# ---------------------------------------------------------------------- post


def post_langs():
    """The UI language as the post language, so language-filtered feeds put the
    post where it belongs. LANG=de_DE.UTF-8 becomes ["de"]; C/POSIX gives none."""
    raw = os.environ.get("LC_ALL") or os.environ.get("LC_MESSAGES") or os.environ.get("LANG") or ""
    code = raw.split(".")[0].split("_")[0].split("@")[0].lower()
    return [code] if re.match(r"^[a-z]{2,3}$", code) and code not in ("c",) else []


def build_post(spec, resolve=resolve_handle, card=link_card):
    """The app.bsky.feed.post record for what the panel composed.

    Everything in the spec is re-validated: the text against the server's own
    limits, the reply reference and every blob reference structurally.
    """
    if not isinstance(spec, dict):
        raise HelperError("bad_post", EXIT_USAGE)
    text = str(spec.get("text") or "").strip()
    images = spec.get("images") or []
    if not isinstance(images, list) or len(images) > MAX_IMAGES:
        raise HelperError("too_many_images", EXIT_USAGE)
    if not text and not images:
        raise HelperError("empty_post", EXIT_USAGE)
    if grapheme_count(text) > MAX_POST_GRAPHEMES or len(text.encode("utf-8")) > MAX_POST_BYTES:
        raise HelperError("post_too_long", EXIT_USAGE)

    record = {"$type": "app.bsky.feed.post", "text": text, "createdAt": now_iso()}
    langs = post_langs()
    if langs:
        record["langs"] = langs
    facets = detect_facets(text, resolve) if text else []
    if facets:
        record["facets"] = facets

    reply = spec.get("reply")
    if reply:
        if not isinstance(reply, dict) or not isinstance(reply.get("parent"), dict) \
                or not isinstance(reply.get("root"), dict):
            raise HelperError("bad_reply", EXIT_USAGE)
        record["reply"] = {
            "root": strong_ref(reply["root"].get("uri"), reply["root"].get("cid")),
            "parent": strong_ref(reply["parent"].get("uri"), reply["parent"].get("cid")),
        }

    if images:
        embedded = []
        for entry in images:
            if not isinstance(entry, dict):
                raise HelperError("bad_blob", EXIT_USAGE)
            image = {"image": check_blob(entry.get("blob")),
                     "alt": str(entry.get("alt") or "")[:10000]}
            aspect = check_aspect(entry.get("aspectRatio"))
            if aspect:
                image["aspectRatio"] = aspect
            embedded.append(image)
        record["embed"] = {"$type": "app.bsky.embed.images", "images": embedded}
    else:
        for facet in facets:
            feature = facet["features"][0]
            if feature["$type"] == "app.bsky.richtext.facet#link":
                embed = card(feature["uri"])
                if embed:
                    record["embed"] = embed
                break
    return record


# ---------------------------------------------------------------- output


def emit(data):
    sys.stdout.write(json.dumps(data))
    sys.stdout.flush()


def optional_cursor(args, name):
    if len(args) > 1:
        raise HelperError("usage: %s [cursor]" % name, EXIT_USAGE)
    cursor = args[0] if args else ""
    if len(cursor) > 1024 or any(ch.isspace() for ch in cursor):
        raise HelperError("bad_cursor", EXIT_USAGE)
    return cursor


def paged(params, cursor):
    if cursor:
        params["cursor"] = cursor
    return params


# -------------------------------------------------------------- subcommands


def cmd_load(_args):
    emit(public_state(read_state()))


def cmd_login(args):
    if len(args) != 2:
        raise HelperError("usage: login <service> <identifier>", EXIT_USAGE)
    service = normalize_service(args[0])
    identifier = args[1].strip().lstrip("@")
    if not identifier or len(identifier) > 256 or any(ch.isspace() for ch in identifier):
        raise HelperError("bad_identifier", EXIT_USAGE)
    password = os.environ.get("BLUESKY_APP_PASSWORD", "")
    if not password:
        raise HelperError("missing_password", EXIT_USAGE)
    try:
        session = raw_call(service, "POST", "com.atproto.server.createSession",
                           body={"identifier": identifier, "password": password})
    except XrpcError as error:
        if error.name == "AuthFactorTokenRequired":
            raise HelperError("auth_factor_required", EXIT_HTTP) from None
        if error.status == 401:
            raise HelperError("invalid_credentials", EXIT_HTTP) from None
        if error.name == "AccountTakedown":
            raise HelperError("account_takedown", EXIT_HTTP) from None
        raise HelperError("http_%d" % error.status, EXIT_HTTP) from None
    state = {"auth": empty_auth()}
    state["auth"]["service"] = service
    store_session(state, session)
    write_state(state)
    emit(public_state(state))


def cmd_logout(_args):
    state = read_state()
    auth = state["auth"]
    # Revoking the session on the server is a courtesy: logging out has to work
    # offline too, so a failure here never stops the local tokens from going.
    if auth["refreshJwt"]:
        try:
            raw_call(auth["pds"] or auth["service"], "POST",
                     "com.atproto.server.deleteSession", token=auth["refreshJwt"])
        except (HelperError, XrpcError, ValueError):
            pass
    write_state({"auth": empty_auth()})
    emit(public_state({"auth": empty_auth()}))


def cmd_timeline(args):
    cursor = optional_cursor(args, "timeline")
    emit(call("GET", "app.bsky.feed.getTimeline", params=paged({"limit": PAGE_SIZE}, cursor)))


def cmd_discover(args):
    cursor = optional_cursor(args, "discover")
    emit(call("GET", "app.bsky.feed.getFeed",
              params=paged({"feed": DISCOVER_FEED, "limit": PAGE_SIZE}, cursor)))


def cmd_mentions(args):
    """Mentions, replies and quotes, in the same shape as a timeline page.

    Notifications only carry the raw record, without counts, embeds or the
    viewer's own like and repost, so the posts are hydrated with getPosts and
    returned as {feed: [{post, notificationReason}], cursor}. A post that is
    gone by now (deleted, or the author blocked) is simply left out.
    """
    cursor = optional_cursor(args, "mentions")
    params = paged({"limit": MENTIONS_PAGE_SIZE, "reasons": list(MENTION_REASONS)}, cursor)
    page = call("GET", "app.bsky.notification.listNotifications", params=params)
    notifications = page.get("notifications") if isinstance(page, dict) else None
    if not isinstance(notifications, list):
        raise HelperError("invalid_response", EXIT_HTTP)
    wanted = []
    reasons = {}
    for entry in notifications:
        if not isinstance(entry, dict) or entry.get("reason") not in MENTION_REASONS:
            continue
        uri = str(entry.get("uri") or "")
        if uri.startswith("at://") and uri not in reasons:
            wanted.append(uri)
            reasons[uri] = entry["reason"]
    posts = {}
    if wanted:
        hydrated = call("GET", "app.bsky.feed.getPosts", params={"uris": wanted[:25]})
        for post in hydrated.get("posts", []) if isinstance(hydrated, dict) else []:
            if isinstance(post, dict) and post.get("uri"):
                posts[post["uri"]] = post
    feed = [{"post": posts[uri], "notificationReason": reasons[uri]}
            for uri in wanted if uri in posts]
    # The cursor belongs to the notification list, not to the hydrated page: a
    # page whose posts were all deleted is still not the end of the list.
    emit({"feed": feed, "cursor": str(page.get("cursor") or ""),
          "count": len(notifications)})


def cmd_like(args):
    if len(args) != 2:
        raise HelperError("usage: like <uri> <cid>", EXIT_USAGE)
    created = create_record("app.bsky.feed.like", {
        "$type": "app.bsky.feed.like", "subject": strong_ref(args[0], args[1]),
        "createdAt": now_iso()})
    emit({"uri": str(created.get("uri") or "")})


def cmd_unlike(args):
    if len(args) != 1:
        raise HelperError("usage: unlike <like-uri>", EXIT_USAGE)
    emit(delete_own_record(args[0], "app.bsky.feed.like"))


def cmd_repost(args):
    if len(args) != 2:
        raise HelperError("usage: repost <uri> <cid>", EXIT_USAGE)
    created = create_record("app.bsky.feed.repost", {
        "$type": "app.bsky.feed.repost", "subject": strong_ref(args[0], args[1]),
        "createdAt": now_iso()})
    emit({"uri": str(created.get("uri") or "")})


def cmd_unrepost(args):
    if len(args) != 1:
        raise HelperError("usage: unrepost <repost-uri>", EXIT_USAGE)
    emit(delete_own_record(args[0], "app.bsky.feed.repost"))


def cmd_follow(args):
    if len(args) != 1:
        raise HelperError("usage: follow <did>", EXIT_USAGE)
    created = create_record("app.bsky.graph.follow", {
        "$type": "app.bsky.graph.follow", "subject": check_did(args[0]),
        "createdAt": now_iso()})
    emit({"uri": str(created.get("uri") or "")})


def cmd_unfollow(args):
    if len(args) != 1:
        raise HelperError("usage: unfollow <follow-uri>", EXIT_USAGE)
    emit(delete_own_record(args[0], "app.bsky.graph.follow"))


def cmd_bookmark(args):
    if len(args) != 2:
        raise HelperError("usage: bookmark <uri> <cid>", EXIT_USAGE)
    call("POST", "app.bsky.bookmark.createBookmark", body=strong_ref(args[0], args[1]))
    emit({"ok": True})


def cmd_unbookmark(args):
    if len(args) != 1:
        raise HelperError("usage: unbookmark <uri>", EXIT_USAGE)
    parse_at_uri(args[0])
    call("POST", "app.bsky.bookmark.deleteBookmark", body={"uri": args[0]})
    emit({"ok": True})


def cmd_upload(args):
    # No argument at all, so a path cannot be passed on the command line by
    # accident: the only channel is the environment.
    if args:
        raise HelperError("usage: upload", EXIT_USAGE)
    path = os.environ.get("BLUESKY_UPLOAD_PATH", "")
    if not path:
        raise HelperError("missing_upload_path", EXIT_USAGE)
    session_state()
    payload, mime = read_local_image(path)
    data, mime = prepare_image(payload, mime)
    blob = check_blob(upload_blob(data, mime))
    size = image_size(data)
    emit({"blob": blob,
          "aspectRatio": {"width": size[0], "height": size[1]} if size else None})


def cmd_post(args):
    if args:
        raise HelperError("usage: post", EXIT_USAGE)
    raw = os.environ.get("BLUESKY_POST_JSON", "")
    if not raw:
        raise HelperError("missing_post_json", EXIT_USAGE)
    try:
        spec = json.loads(raw)
    except ValueError:
        raise HelperError("bad_post", EXIT_USAGE) from None
    record = build_post(spec)
    created = create_record("app.bsky.feed.post", record)
    emit({"uri": str(created.get("uri") or ""), "cid": str(created.get("cid") or "")})


COMMANDS = {
    "load": cmd_load,
    "login": cmd_login,
    "logout": cmd_logout,
    "timeline": cmd_timeline,
    "discover": cmd_discover,
    "mentions": cmd_mentions,
    "like": cmd_like,
    "unlike": cmd_unlike,
    "repost": cmd_repost,
    "unrepost": cmd_unrepost,
    "follow": cmd_follow,
    "unfollow": cmd_unfollow,
    "bookmark": cmd_bookmark,
    "unbookmark": cmd_unbookmark,
    "upload": cmd_upload,
    "post": cmd_post,
}


def main(argv):
    if len(argv) < 2 or argv[1] not in COMMANDS:
        sys.stderr.write("usage: bluesky_helper.py <%s> [args]\n" % "|".join(sorted(COMMANDS)))
        return EXIT_USAGE
    try:
        COMMANDS[argv[1]](argv[2:])
    except HelperError as error:
        # A refused request exits non-zero with an empty stdout, so the panel
        # never mistakes an error for a result.
        sys.stderr.write("BLUESKY_ERROR:" + error.message + "\n")
        return error.status
    except XrpcError as error:
        sys.stderr.write("BLUESKY_ERROR:http_%d\n" % error.status)
        return EXIT_HTTP
    except ValueError:
        sys.stderr.write("BLUESKY_ERROR:invalid_response\n")
        return EXIT_HTTP
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
