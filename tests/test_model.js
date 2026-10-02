// Guards the properties that matter most: no command line the panel builds may
// contain a credential, a shell or a host it has no business talking to, and
// nothing a stranger writes in a post may turn into markup or a dangerous link.
//
// Run with: node tests/test_model.js

const assert = require("assert")
const fs = require("fs")
const path = require("path")
const childProcess = require("child_process")
const Model = require(path.join(__dirname, "..", "Model.js"))

const ROOT = path.join(__dirname, "..")
const HELPER = "/plugin/bluesky_helper.py"
const SECRET_LIKE = [
  "accessJwt", "refreshJwt", "password", "Authorization", "Bearer",
  "BLUESKY_APP_PASSWORD",
]
const URI = "at://did:plc:abcdefghijklmnop/app.bsky.feed.post/3kabc"
const CID = "bafyreiabcdefghijklmnop"

const fixture = function (name) {
  return JSON.parse(fs.readFileSync(path.join(__dirname, "fixtures", name), "utf8"))
}

let passed = 0
function test(name, body) {
  try {
    body()
    passed++
  } catch (error) {
    console.error("FAIL: " + name)
    console.error("      " + error.message)
    process.exitCode = 1
  }
}

// Every builder in the plugin. When you add one, add it here.
const COMMANDS = {
  "loadCmd": Model.loadCmd(HELPER),
  "logoutCmd": Model.logoutCmd(HELPER),
  "timelineCmd": Model.timelineCmd(HELPER),
  "timelineCmd paged": Model.timelineCmd(HELPER, "1727830000000::bafy"),
  "discoverCmd": Model.discoverCmd(HELPER, "cursor-2"),
  "mentionsCmd": Model.mentionsCmd(HELPER),
  "likeCmd": Model.likeCmd(HELPER, URI, CID),
  "unlikeCmd": Model.unlikeCmd(HELPER, "at://did:plc:me/app.bsky.feed.like/3kxyz"),
  "repostCmd": Model.repostCmd(HELPER, URI, CID),
  "unrepostCmd": Model.unrepostCmd(HELPER, "at://did:plc:me/app.bsky.feed.repost/3kxyz"),
  "followCmd": Model.followCmd(HELPER, "did:plc:abcdefghijklmnop"),
  "unfollowCmd": Model.unfollowCmd(HELPER, "at://did:plc:me/app.bsky.graph.follow/3kxyz"),
  "bookmarkCmd": Model.bookmarkCmd(HELPER, URI, CID),
  "unbookmarkCmd": Model.unbookmarkCmd(HELPER, URI),
  "uploadMediaCmd": Model.uploadMediaCmd(HELPER),
  "postCmd": Model.postCmd(HELPER),
}
// Login is the one command that names a server, on purpose.
const LOGIN = Model.loginCmd(HELPER, "", "alice.bsky.social")

test("every command starts with the helper and has no shell", function () {
  for (const name in Object.assign({ loginCmd: LOGIN }, COMMANDS)) {
    const cmd = name === "loginCmd" ? LOGIN : COMMANDS[name]
    assert.ok(Array.isArray(cmd), name + " must be an array")
    assert.strictEqual(cmd[0], HELPER, name + " must not shell out")
    for (const part of cmd) {
      assert.ok(!/^(sh|bash|curl|wget|\/bin\/)/.test(part),
        name + " must not contain a shell or an http client: " + part)
    }
  }
})

test("no command line mentions a credential", function () {
  for (const name in Object.assign({ loginCmd: LOGIN }, COMMANDS)) {
    const joined = (name === "loginCmd" ? LOGIN : COMMANDS[name]).join(" ")
    for (const secret of SECRET_LIKE) {
      assert.ok(joined.indexOf(secret) === -1, name + " leaks " + secret + ": " + joined)
    }
  }
})

test("no command line but login names a host", function () {
  for (const name in COMMANDS) {
    for (const part of COMMANDS[name]) {
      assert.ok(!/https?:\/\//.test(part), name + " carries a URL: " + part)
    }
  }
})

test("login carries the server and the handle, never the password", function () {
  assert.deepStrictEqual(LOGIN, [HELPER, "login", "https://bsky.social", "alice.bsky.social"])
  assert.deepStrictEqual(Model.loginCmd(HELPER, "pds.example.org", "@bob.example.org"),
    [HELPER, "login", "https://pds.example.org", "bob.example.org"])
  assert.strictEqual(Model.loginCmd.length, 3, "loginCmd must not take a password")
})

test("upload and post are bare subcommands", function () {
  assert.deepStrictEqual(Model.uploadMediaCmd(HELPER), [HELPER, "upload"])
  assert.deepStrictEqual(Model.postCmd(HELPER), [HELPER, "post"])
})

// ----------------------------------------------------------------- service

test("an empty server means bsky.social", function () {
  assert.strictEqual(Model.normalizeService(""), "https://bsky.social")
  assert.strictEqual(Model.normalizeService("   "), "https://bsky.social")
})

test("a bare host becomes https, and a path is dropped", function () {
  assert.strictEqual(Model.normalizeService("pds.example.org"), "https://pds.example.org")
  assert.strictEqual(Model.normalizeService("https://pds.example.org/xrpc/"), "https://pds.example.org")
  assert.strictEqual(Model.normalizeService("PDS.Example.org:8443"), "https://pds.example.org:8443")
})

test("plaintext is refused except for loopback", function () {
  assert.strictEqual(Model.normalizeService("http://pds.example.org"), "")
  assert.strictEqual(Model.normalizeService("http://127.0.0.1.evil.example"), "")
  assert.strictEqual(Model.normalizeService("http://localhost:2583"), "http://localhost:2583")
  assert.strictEqual(Model.normalizeService("http://127.0.0.1:2583"), "http://127.0.0.1:2583")
})

test("other schemes and userinfo are refused", function () {
  assert.strictEqual(Model.normalizeService("javascript:alert(1)"), "")
  assert.strictEqual(Model.normalizeService("mailto:x@example.org"), "")
  assert.strictEqual(Model.normalizeService("ftp://pds.example.org"), "")
  assert.strictEqual(Model.normalizeService("bsky.social@evil.example"), "")
  assert.strictEqual(Model.normalizeService("https://bsky.social@evil.example/"), "")
})

// -------------------------------------------------------------------- auth

test("the panel view of the credentials holds no token", function () {
  const auth = Model.decodeAuth({
    service: "https://bsky.social", handle: "alice.bsky.social", did: "did:plc:a",
    hasSession: true, accessJwt: "secret", refreshJwt: "secret",
  })
  assert.deepStrictEqual(Object.keys(auth).sort(), ["did", "handle", "hasSession", "service"])
  assert.ok(JSON.stringify(auth).indexOf("secret") === -1)
})

test("authed needs a DID and a session", function () {
  assert.strictEqual(Model.isAuthed({ did: "did:plc:a", hasSession: true }), true)
  assert.strictEqual(Model.isAuthed({ did: "did:plc:a", hasSession: false }), false)
  assert.strictEqual(Model.isAuthed({ did: "", hasSession: true }), false)
  assert.strictEqual(Model.isAuthed({ did: "did:plc:a", hasSession: "true" }), false)
  assert.strictEqual(Model.isAuthed(null), false)
})

test("helper errors are turned into something a person can act on", function () {
  assert.strictEqual(Model.errorName("BLUESKY_ERROR:invalid_credentials\n"), "invalid_credentials")
  assert.strictEqual(Model.errorName(""), "")
  assert.ok(/app password/i.test(Model.loginErrorMessage("auth_factor_required")))
  assert.ok(/pds\.example\.org/.test(Model.loginErrorMessage("network_error", "https://pds.example.org")))
  assert.ok(/2 MB/.test(Model.uploadErrorMessage("image_too_large", "/home/me/a.jpg")))
  assert.ok(/a\.jpg/.test(Model.uploadErrorMessage("image_too_large", "/home/me/a.jpg")))
})

// ----------------------------------------------------- counting and limits

test("graphemes are counted the way the server counts them", function () {
  assert.strictEqual(Model.graphemeCount(""), 0)
  assert.strictEqual(Model.graphemeCount("hello"), 5)
  assert.strictEqual(Model.graphemeCount("über"), 4)
  assert.strictEqual(Model.graphemeCount("é"), 1, "a combining accent")
  assert.strictEqual(Model.graphemeCount("👍🏽"), 1, "a skin tone modifier")
  assert.strictEqual(Model.graphemeCount("🇩🇪🇫🇷"), 2, "two flags")
  assert.strictEqual(Model.graphemeCount("👨‍👩‍👧"), 1, "a ZWJ family")
  assert.strictEqual(Model.graphemeCount("❤️"), 1, "a variation selector")
})

test("bytes are counted as UTF-8", function () {
  assert.strictEqual(Model.byteLength("abc"), 3)
  assert.strictEqual(Model.byteLength("ü"), 2)
  assert.strictEqual(Model.byteLength("€"), 3)
  assert.strictEqual(Model.byteLength("😀"), 4)
})

test("the composer is cut on a grapheme boundary", function () {
  assert.strictEqual(Model.limitText("abcdef", 3, 0), "abc")
  assert.strictEqual(Model.limitText("ab👍🏽cd", 3, 0), "ab👍🏽")
  assert.strictEqual(Model.limitText("🇩🇪🇫🇷🇮🇹", 2, 0), "🇩🇪🇫🇷")
  assert.strictEqual(Model.limitText("short", 300, 3000), "short")
})

test("the byte limit applies too", function () {
  const text = "😀".repeat(300)
  assert.strictEqual(Model.graphemeCount(text), 300)
  const cut = Model.limitText(text, 300, 1000)
  assert.ok(Model.byteLength(cut) <= 1000)
  assert.strictEqual(cut, "😀".repeat(250))
})

test("a nonsensical limit leaves the text alone", function () {
  assert.strictEqual(Model.limitText("abc", 0, 0), "abc")
  assert.strictEqual(Model.limitText("abc", "x", 0), "abc")
  assert.strictEqual(Model.limitText(null, 10, 0), "")
})

test("the counting matches the helper's", function () {
  const samples = ["", "hello", "é", "👍🏽", "🇩🇪🇫🇷", "👨‍👩‍👧", "❤️", "Grüße 🇩🇪 #tag", "a‍b"]
  const script = "import json,sys; sys.path.insert(0, sys.argv[1]); import bluesky_helper as h;" +
    "print(json.dumps([h.grapheme_count(s) for s in json.loads(sys.argv[2])]))"
  const out = childProcess.execFileSync("python3", ["-c", script, ROOT, JSON.stringify(samples)],
    { env: Object.assign({}, process.env, { PYTHONDONTWRITEBYTECODE: "1" }) })
  assert.deepStrictEqual(JSON.parse(String(out)), samples.map(Model.graphemeCount))
})

// ----------------------------------------------------------------- composing

test("a reply to a top-level post uses it as root and parent", function () {
  const ref = Model.replyRefFor({ uri: URI, cid: CID, record: { text: "hi" } })
  assert.deepStrictEqual(ref, { root: { uri: URI, cid: CID }, parent: { uri: URI, cid: CID } })
})

test("a reply to a reply keeps the thread's root", function () {
  const root = { uri: "at://did:plc:r/app.bsky.feed.post/1", cid: "bafyroot" }
  const ref = Model.replyRefFor({ uri: URI, cid: CID, record: { reply: { root: root, parent: root } } })
  assert.deepStrictEqual(ref.root, root)
  assert.deepStrictEqual(ref.parent, { uri: URI, cid: CID })
  assert.strictEqual(Model.replyRefFor({ uri: URI }), null)
})

test("only uploaded images go into a post, with their alt text", function () {
  const blob = { $type: "blob", ref: { $link: "bafkrei" }, mimeType: "image/jpeg", size: 10 }
  const media = [
    { path: "/a.jpg", blob: blob, description: "a cat", aspectRatio: { width: 4, height: 3 } },
    { path: "/b.jpg", blob: null, failed: true, description: "" },
  ]
  assert.deepStrictEqual(Model.uploadedImages(media),
    [{ blob: blob, alt: "a cat", aspectRatio: { width: 4, height: 3 } }])
  // A QML list is not an Array, so the helpers must not rely on isArray().
  const qmlList = { length: 1, 0: media[0] }
  assert.strictEqual(Model.uploadedImages(qmlList).length, 1)
})

test("the post spec carries text, reply and images, and is cut once more", function () {
  const blob = { $type: "blob", ref: { $link: "bafkrei" }, mimeType: "image/png", size: 10 }
  const ref = Model.replyRefFor({ uri: URI, cid: CID, record: {} })
  const spec = JSON.parse(Model.postSpec("  hello  ", ref, [{ blob: blob, description: "" }]))
  assert.strictEqual(spec.text, "hello")
  assert.deepStrictEqual(spec.reply, ref)
  assert.strictEqual(spec.images.length, 1)
  const long = JSON.parse(Model.postSpec("x".repeat(400), null, []))
  assert.strictEqual(long.text.length, 300)
  assert.ok(!("reply" in long) && !("images" in long))
})

test("a local file becomes a file URL with its special characters escaped", function () {
  assert.strictEqual(Model.fileUrl("/home/me/my pic#1.png"), "file:///home/me/my%20pic%231.png")
  assert.strictEqual(Model.fileUrl("relative.png"), "")
  assert.strictEqual(Model.baseName("/home/me/pic.png"), "pic.png")
})

// -------------------------------------------------------------- the feeds

test("real feed pages parse", function () {
  const author = Model.feedPage(fixture("author_feed.json"))
  const discover = Model.feedPage(fixture("discover_feed.json"))
  assert.strictEqual(author.items.length, 30)
  assert.strictEqual(discover.items.length, 40)
  assert.ok(author.cursor !== "")
  assert.strictEqual(Model.feedPage({ error: "x" }), null)
  assert.strictEqual(Model.feedPage(null), null)
  assert.strictEqual(Model.feedPage({ feed: [] }).cursor, "")
})

test("images, videos, link cards and quotes are found in real posts", function () {
  const counts = { media: 0, video: 0, external: 0, quote: 0, note: 0 }
  const items = Model.feedPage(fixture("author_feed.json")).items
    .concat(Model.feedPage(fixture("discover_feed.json")).items)
  for (const item of items) {
    const media = Model.postMedia(item.post)
    counts.media += media.length
    counts.video += media.filter(function (m) { return m.video }).length
    if (Model.postExternal(item.post)) counts.external++
    const quote = Model.postQuote(item.post)
    if (quote && quote.author) counts.quote++
    if (quote && !quote.author) counts.note++
    for (const m of media) {
      assert.ok(/^https:\/\//.test(m.url), "media must be https: " + m.url)
      if (m.video) assert.ok(/^https:\/\/bsky\.app\/profile\//.test(m.link))
    }
  }
  assert.ok(counts.media >= 30, "media: " + counts.media)
  assert.ok(counts.video >= 1, "video: " + counts.video)
  assert.ok(counts.external >= 3, "external: " + counts.external)
  assert.ok(counts.quote >= 10, "quote: " + counts.quote)
  // The fixture quotes a starter pack, which is not a post.
  assert.ok(counts.note >= 1, "note: " + counts.note)
})

test("a feed never shows more than four images per post", function () {
  const images = []
  for (let i = 0; i < 6; i++) images.push({ thumb: "https://cdn.example/" + i, fullsize: "https://cdn.example/f" + i, alt: "" })
  const post = { embed: { $type: "app.bsky.embed.images#view", images: images } }
  assert.strictEqual(Model.postMedia(post).length, 4)
})

test("unsafe media URLs are dropped", function () {
  const post = { embed: { $type: "app.bsky.embed.images#view", images: [
    { thumb: "javascript:alert(1)", fullsize: "file:///etc/passwd", alt: "" },
    { thumb: "https://cdn.example/ok", fullsize: "https://cdn.example/ok-full", alt: "fine" },
  ] } }
  assert.deepStrictEqual(Model.postMedia(post).map(function (m) { return m.url }), ["https://cdn.example/ok"])
  assert.strictEqual(Model.postExternal({ embed: { $type: "app.bsky.embed.external#view",
    external: { uri: "javascript:alert(1)", title: "x" } } }), null)
})

test("media of a quote with media is shown, and so is the quote", function () {
  const post = { embed: { $type: "app.bsky.embed.recordWithMedia#view",
    media: { $type: "app.bsky.embed.images#view", images: [{ thumb: "https://cdn.example/t", fullsize: "https://cdn.example/f", alt: "" }] },
    record: { record: { $type: "app.bsky.embed.record#viewRecord", uri: URI,
      author: { did: "did:plc:q", handle: "q.example" }, value: { text: "quoted" } } } } }
  assert.strictEqual(Model.postMedia(post).length, 1)
  assert.strictEqual(Model.postQuote(post).text, "quoted")
  assert.strictEqual(Model.postQuote(post).url, "https://bsky.app/profile/q.example/post/3kabc")
})

test("deleted and blocked quotes become a note", function () {
  const quote = function (type) {
    return Model.postQuote({ embed: { $type: "app.bsky.embed.record#view", record: { $type: type } } })
  }
  assert.ok(/deleted/.test(quote("app.bsky.embed.record#viewNotFound").note))
  assert.ok(/blocked/.test(quote("app.bsky.embed.record#viewBlocked").note))
})

test("reposts, replies and mentions are labelled", function () {
  const items = Model.feedPage(fixture("author_feed.json")).items
  assert.ok(items.some(function (i) { return Model.reposter(i) !== null }))
  assert.ok(items.some(function (i) { return Model.replyParentAuthor(i) !== null }))
  assert.strictEqual(Model.notificationLabel({ notificationReason: "reply" }), "replied to you")
  assert.strictEqual(Model.notificationLabel({ notificationReason: "quote" }), "quoted you")
  assert.strictEqual(Model.notificationLabel({}), "")
})

test("a post and its repost are two entries, a repeated page is one", function () {
  const post = { uri: URI, cid: CID }
  const plain = { post: post }
  const repost = { post: post, reason: { $type: "app.bsky.feed.defs#reasonRepost", by: { did: "did:plc:r" } } }
  assert.notStrictEqual(Model.entryKey(plain), Model.entryKey(repost))
  assert.strictEqual(Model.appendUnique([plain], [plain, repost]).length, 2)
  assert.strictEqual(Model.appendUnique([], null).length, 0)
})

test("labels hide media", function () {
  assert.strictEqual(Model.isSensitive({ labels: [{ val: "porn" }] }), true)
  assert.strictEqual(Model.isSensitive({ labels: [{ val: "graphic-media" }] }), true)
  assert.strictEqual(Model.isSensitive({ labels: [{ val: "!no-unauthenticated" }] }), false)
  assert.strictEqual(Model.isSensitive({ labels: [] }), false)
})

test("counts are short", function () {
  assert.strictEqual(Model.count(0), "")
  assert.strictEqual(Model.count(999), "999")
  assert.strictEqual(Model.count(10297), "10.2K")
  assert.strictEqual(Model.count(2500000), "2.5M")
})

test("a post links to bsky.app, a DID keeps its colons", function () {
  assert.strictEqual(Model.postWebUrl({ uri: URI, author: { handle: "alice.bsky.social" } }),
    "https://bsky.app/profile/alice.bsky.social/post/3kabc")
  assert.strictEqual(Model.postWebUrl({ uri: URI, author: { handle: "handle.invalid", did: "did:plc:x" } }),
    "https://bsky.app/profile/did:plc:x/post/3kabc")
  assert.strictEqual(Model.handle({ handle: "handle.invalid", did: "did:plc:x" }), "did:plc:x")
})

// --------------------------------------------------------------- rich text

test("facets become links at the right characters", function () {
  const text = "Grüße an @alice.example und #Omarchy https://example.org"
  // Offsets are UTF-8 bytes, which is what makes "Grüße" worth testing with.
  const span = function (piece) {
    const start = Buffer.byteLength(text.substring(0, text.indexOf(piece)))
    return { byteStart: start, byteEnd: start + Buffer.byteLength(piece) }
  }
  const record = {
    text: text,
    facets: [
      { index: span("@alice.example"), features: [{ $type: "app.bsky.richtext.facet#mention", did: "did:plc:alice" }] },
      { index: span("#Omarchy"), features: [{ $type: "app.bsky.richtext.facet#tag", tag: "Omarchy" }] },
      { index: span("https://example.org"), features: [{ $type: "app.bsky.richtext.facet#link", uri: "https://example.org" }] },
    ],
  }
  const links = Model.segments(record).filter(function (s) { return s.url })
  assert.deepStrictEqual(links.map(function (s) { return s.text }), ["@alice.example", "#Omarchy", "https://example.org"])
  assert.deepStrictEqual(links.map(function (s) { return s.url }), [
    "https://bsky.app/profile/did:plc:alice",
    "https://bsky.app/hashtag/Omarchy",
    "https://example.org",
  ])
  assert.strictEqual(Model.segments(record).map(function (s) { return s.text }).join(""), record.text)
})

test("real facets land on the text they describe", function () {
  const items = Model.feedPage(fixture("author_feed.json")).items
  let checked = 0
  for (const item of items) {
    for (const segment of Model.segments(item.post.record)) {
      if (!segment.url) continue
      checked++
      if (/\/hashtag\//.test(segment.url)) assert.ok(/^[#＃]/.test(segment.text), segment.text)
      if (/\/profile\/did:/.test(segment.url)) assert.ok(/^@/.test(segment.text), segment.text)
    }
  }
  assert.ok(checked >= 10, "checked " + checked)
})

test("a facet with a broken index is skipped, not guessed at", function () {
  const record = { text: "ü!", facets: [{ index: { byteStart: 1, byteEnd: 3 },
    features: [{ $type: "app.bsky.richtext.facet#link", uri: "https://example.org" }] }] }
  assert.ok(Model.segments(record).every(function (s) { return s.url === "" }))
})

test("nothing in a post becomes markup", function () {
  const html = Model.postRichText({ text: '<img src="https://tracker.example/x"> & <b>bold</b>\nline' }, "#fff", "#08f")
  assert.ok(html.indexOf("<img") === -1)
  assert.ok(html.indexOf("<b>") === -1)
  assert.ok(html.indexOf("&lt;img") !== -1)
  assert.ok(html.indexOf("<br>") !== -1)
})

test("a link facet with a dangerous target stays text", function () {
  const record = { text: "click", facets: [{ index: { byteStart: 0, byteEnd: 5 },
    features: [{ $type: "app.bsky.richtext.facet#link", uri: "javascript:alert(1)" }] }] }
  assert.ok(Model.postRichText(record, "#fff", "#08f").indexOf("<a ") === -1)
  const quoted = { text: "x", facets: [{ index: { byteStart: 0, byteEnd: 1 },
    features: [{ $type: "app.bsky.richtext.facet#link", uri: 'https://e.example/"onmouseover="x' }] }] }
  assert.ok(Model.postRichText(quoted, "#fff", "#08f").indexOf('"onmouseover') === -1)
})

// ------------------------------------------------------ updating in place

test("a like is applied to the post on screen", function () {
  const list = [{ post: { uri: URI, likeCount: 2, viewer: {} } }, { post: { uri: "other", likeCount: 1 } }]
  const liked = Model.patchPost(list, URI, { like: "at://did:plc:me/app.bsky.feed.like/1" }, { likeCount: 1 })
  assert.strictEqual(liked[0].post.likeCount, 3)
  assert.strictEqual(liked[0].post.viewer.like, "at://did:plc:me/app.bsky.feed.like/1")
  assert.strictEqual(liked[1], list[1], "other posts are left alone")
  assert.strictEqual(list[0].post.likeCount, 2, "the original is not mutated")
  const unliked = Model.patchPost(liked, URI, { like: undefined }, { likeCount: -1 })
  assert.ok(!unliked[0].post.viewer.like)
  assert.strictEqual(Model.patchPost([{ post: { uri: URI } }], URI, {}, { likeCount: -1 })[0].post.likeCount, 0)
})

test("a follow applies to every post by that author", function () {
  const list = [
    { post: { uri: "a", author: { did: "did:plc:x", viewer: {} } } },
    { post: { uri: "b", author: { did: "did:plc:x" } } },
    { post: { uri: "c", author: { did: "did:plc:y" } } },
  ]
  const next = Model.patchAuthorFollow(list, "did:plc:x", "at://did:plc:me/app.bsky.graph.follow/1")
  assert.ok(Model.isFollowing(next[0].post.author))
  assert.ok(Model.isFollowing(next[1].post.author))
  assert.ok(!Model.isFollowing(next[2].post.author))
})

// ------------------------------------------------------- source inspection

const PANEL = fs.readFileSync(path.join(ROOT, "Panel.qml"), "utf8")
const WIDGET = fs.readFileSync(path.join(ROOT, "BarWidget.qml"), "utf8")
const HELPER_SOURCE = fs.readFileSync(path.join(ROOT, "bluesky_helper.py"), "utf8")

test("the app password travels on the environment and is cleared again", function () {
  assert.ok(/loginProc\.environment\s*=\s*\(\{\s*BLUESKY_APP_PASSWORD:/.test(WIDGET))
  assert.ok(/loginProc\.command\s*=\s*Model\.loginCmd\(root\.helperScript,\s*normalized,\s*identifier\)/.test(WIDGET))
  assert.ok(/loginProc\.environment\s*=\s*\(\{\}\)/.test(WIDGET), "the password must be dropped after login")
  assert.ok(/passwordField\.text\s*=\s*""/.test(PANEL), "the field must be cleared when the login starts")
})

test("the upload path and the post travel on the environment", function () {
  assert.ok(/uploadMediaProc\.environment\s*=\s*\(\{\s*BLUESKY_UPLOAD_PATH:/.test(PANEL))
  assert.ok(/uploadMediaProc\.command\s*=\s*Model\.uploadMediaCmd\(root\.helperScript\)/.test(PANEL))
  assert.ok(/BLUESKY_POST_JSON:\s*Model\.postSpec\(/.test(PANEL))
  assert.ok(/postProc\.command\s*=\s*Model\.postCmd\(root\.helperScript\)/.test(PANEL))
})

test("the panel sources contain no shell and no curl", function () {
  for (const file of ["BarWidget.qml", "Panel.qml", "Model.js"]) {
    const source = fs.readFileSync(path.join(ROOT, file), "utf8")
    const code = source.split("\n").filter(function (line) {
      const trimmed = line.trim()
      return trimmed.indexOf("//") !== 0 && trimmed.indexOf("*") !== 0
    }).join("\n")
    assert.ok(code.indexOf("curl") === -1, file + " references curl")
    assert.ok(!/\bsh -c\b/.test(code), file + " builds a shell command")
    assert.ok(!/accessJwt|refreshJwt/.test(code), file + " handles a token")
  }
})

test("the limits and defaults match the helper", function () {
  const constant = function (name) {
    const match = new RegExp("^" + name + " = (.+)$", "m").exec(HELPER_SOURCE)
    assert.ok(match, name + " missing in the helper")
    return match[1].trim().replace(/^"|"$/g, "")
  }
  assert.strictEqual(Number(constant("MAX_POST_GRAPHEMES")), Model.MAX_POST_GRAPHEMES)
  assert.strictEqual(Number(constant("MAX_POST_BYTES")), Model.MAX_POST_BYTES)
  assert.strictEqual(Number(constant("MAX_IMAGES")), Model.MAX_IMAGES)
  assert.strictEqual(constant("DEFAULT_SERVICE"), Model.DEFAULT_SERVICE)
})

test("every helper subcommand has a builder, and every builder a subcommand", function () {
  const helperCommands = (HELPER_SOURCE.match(/^ {4}"([a-z]+)": cmd_\1,$/gm) || [])
    .map(function (line) { return /"([a-z]+)"/.exec(line)[1] }).sort()
  const built = Object.keys(COMMANDS).map(function (name) { return COMMANDS[name][1] })
    .concat([LOGIN[1]])
  const unique = built.filter(function (v, i) { return built.indexOf(v) === i }).sort()
  assert.deepStrictEqual(unique, helperCommands)
})

console.log(passed + " passed" + (process.exitCode ? ", with failures" : ""))
