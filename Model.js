// Pure helpers for the Bluesky panel: command builders, feed parsing, rich
// text, character limits. Nothing in here holds a credential, and every command
// below runs bluesky_helper.py with non-secret arguments only.

var DEFAULT_SERVICE = "https://bsky.social"
var PAGE_SIZE = 40

// The record limits of app.bsky.feed.post, from the lexicon itself: 300
// graphemes and 3000 bytes of UTF-8. Unlike a Mastodon instance a PDS does not
// advertise these, they are part of the protocol.
var MAX_POST_GRAPHEMES = 300
var MAX_POST_BYTES = 3000
var MAX_IMAGES = 4
// The image record has no alt text limit of its own. Bluesky's app stops at
// 2000 characters, and so does this one.
var MAX_ALT_TEXT = 2000

// Labels that hide media behind "Show media", the same set Bluesky's own app
// treats as adult or graphic by default.
var SENSITIVE_LABELS = ["porn", "sexual", "nudity", "graphic-media", "gore", "nsfl"]

var WEB_BASE = "https://bsky.app"

// --------------------------------------------------------------- service
//
// The server a user logs in to. Empty means Bluesky's own entryway; anything
// else is a self-hosted PDS. Only https is accepted, or http to a name that
// really is this machine. userinfo ("bsky.social@evil.example") is refused,
// since it makes a host look like the right one when it is not.

function isLoopbackHost(host) {
  var text = String(host || "").toLowerCase()
  if (text === "") return false
  if (text === "localhost" || text === "::1") return true
  var match = /^(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})$/.exec(text)
  if (!match) return false
  for (var i = 1; i <= 4; i++) {
    if (Number(match[i]) > 255) return false
  }
  return Number(match[1]) === 127
}

function normalizeService(input) {
  var text = String(input || "").trim().toLowerCase()
  if (text === "") return DEFAULT_SERVICE

  // "pds.example.org:8443" is a host and a port, not a scheme: a colon only
  // starts a scheme when it is followed by "//" or by something that is not a
  // port number ("javascript:alert(1)", "mailto:x").
  var explicitScheme = /^([a-z][a-z0-9+.-]*):\/\//i.exec(text)
  if (explicitScheme) {
    if (explicitScheme[1] !== "http" && explicitScheme[1] !== "https") return ""
  } else if (/^[a-z][a-z0-9+.-]*:(?!\d)/i.test(text)) {
    return ""
  } else {
    text = "https://" + text
  }

  var parsed = /^(https?):\/\/([^\/?#]*)/i.exec(text)
  if (!parsed) return ""
  var scheme = parsed[1]
  var authority = parsed[2]
  if (authority === "" || authority.indexOf("@") !== -1) return ""

  var host
  if (authority.charAt(0) === "[") {
    var close = authority.indexOf("]")
    if (close === -1) return ""
    host = authority.substring(1, close)
  } else {
    var colon = authority.indexOf(":")
    host = colon === -1 ? authority : authority.substring(0, colon)
  }
  if (host === "") return ""
  if (scheme === "http" && !isLoopbackHost(host)) return ""
  // XRPC lives on the origin; a path the user typed along with it is dropped.
  return scheme + "://" + authority
}

// A handle is typed with or without the leading @. An email address is a valid
// login identifier as well, so the text is only trimmed, not validated.
function normalizeIdentifier(input) {
  return String(input || "").trim().replace(/^@+/, "")
}

// ------------------------------------------------------- helper commands

function loadCmd(helper) {
  return [helper, "load"]
}

// The app password is not here. It travels in BLUESKY_APP_PASSWORD on the
// process environment, which only the owning user can read.
function loginCmd(helper, service, identifier) {
  return [helper, "login", normalizeService(service), normalizeIdentifier(identifier)]
}

function logoutCmd(helper) {
  return [helper, "logout"]
}

function withCursor(cmd, cursor) {
  if (cursor) cmd.push(String(cursor))
  return cmd
}

function timelineCmd(helper, cursor) {
  return withCursor([helper, "timeline"], cursor)
}

function discoverCmd(helper, cursor) {
  return withCursor([helper, "discover"], cursor)
}

function mentionsCmd(helper, cursor) {
  return withCursor([helper, "mentions"], cursor)
}

function likeCmd(helper, uri, cid) {
  return [helper, "like", String(uri), String(cid)]
}

function unlikeCmd(helper, likeUri) {
  return [helper, "unlike", String(likeUri)]
}

function repostCmd(helper, uri, cid) {
  return [helper, "repost", String(uri), String(cid)]
}

function unrepostCmd(helper, repostUri) {
  return [helper, "unrepost", String(repostUri)]
}

function followCmd(helper, did) {
  return [helper, "follow", String(did)]
}

function unfollowCmd(helper, followUri) {
  return [helper, "unfollow", String(followUri)]
}

function bookmarkCmd(helper, uri, cid) {
  return [helper, "bookmark", String(uri), String(cid)]
}

function unbookmarkCmd(helper, uri) {
  return [helper, "unbookmark", String(uri)]
}

// The path of the picked file is not here: it travels in BLUESKY_UPLOAD_PATH,
// since a filename says what is about to be published and argv is world
// readable.
function uploadMediaCmd(helper) {
  return [helper, "upload"]
}

// The composed post travels in BLUESKY_POST_JSON (see postSpec), so the
// command is the bare subcommand.
function postCmd(helper) {
  return [helper, "post"]
}

// The desktop's own chooser: a QtQuick.Dialogs FileDialog does not open inside
// Quickshell. It goes through the XDG portal, so it is an ordinary window.
function pickMediaCmd() {
  return ["omarchy-file-select", "--title", "Attach images", "--multiple",
    "--extensions", "jpg jpeg png gif webp"]
}

// --------------------------------------------------------- composing

// The reply reference a post needs: the parent is the post being answered, the
// root is the start of its thread — which is the parent itself unless the
// parent is a reply too.
function replyRefFor(post) {
  if (!post || !post.uri || !post.cid) return null
  var parent = { uri: String(post.uri), cid: String(post.cid) }
  var record = post.record || {}
  var root = record.reply && record.reply.root && record.reply.root.uri && record.reply.root.cid
    ? { uri: String(record.reply.root.uri), cid: String(record.reply.root.cid) }
    : parent
  return { root: root, parent: parent }
}

// The images of the composer that made it to the server, in the shape the
// helper turns into an app.bsky.embed.images. The alt text is part of the post
// record on Bluesky, not of the upload, so it is collected here at post time.
// QML passes arrays as QVariantList, for which Array.isArray() is false, so
// this duck-types on length.
function uploadedImages(media) {
  var out = []
  if (!media || typeof media.length !== "number") return out
  for (var i = 0; i < media.length; i++) {
    var entry = media[i]
    if (!entry || !entry.blob) continue
    out.push({
      blob: entry.blob,
      alt: limitText(String(entry.description || ""), MAX_ALT_TEXT, 0),
      aspectRatio: entry.aspectRatio || null
    })
  }
  return out
}

// What the helper receives in BLUESKY_POST_JSON. The text is cut once more
// here, the one place every post passes through.
function postSpec(text, reply, media) {
  var spec = { text: limitText(String(text || "").trim(), MAX_POST_GRAPHEMES, MAX_POST_BYTES) }
  if (reply && reply.root && reply.parent) spec.reply = reply
  var images = uploadedImages(media)
  if (images.length > 0) spec.images = images.slice(0, MAX_IMAGES)
  return JSON.stringify(spec)
}

function baseName(path) {
  var text = String(path || "")
  var cut = text.lastIndexOf("/")
  return cut === -1 ? text : text.substring(cut + 1)
}

// A local file as an Image source. Only absolute paths, and the path is
// percent-encoded so a "#" or "?" in a filename is not read as part of a URL.
function fileUrl(path) {
  var text = String(path || "")
  if (text.charAt(0) !== "/") return ""
  return "file://" + text.split("/").map(encodeURIComponent).join("/")
}

// --------------------------------------------------- counting and limits
//
// The server counts graphemes. The QML engine has no Intl.Segmenter, so this
// walks code points and lets those that only ever extend the previous
// character ride along: combining marks, variation selectors, skin tone
// modifiers, tag characters, and whatever follows a zero width joiner. Two
// regional indicators make one flag. Where this is wrong it counts more, never
// less, so a post the counter accepts is one the server accepts.
// bluesky_helper.py counts the same way.

function isExtender(code) {
  return (code >= 0x0300 && code <= 0x036F) || (code >= 0x1AB0 && code <= 0x1AFF)
    || (code >= 0x1DC0 && code <= 0x1DFF) || (code >= 0x20D0 && code <= 0x20FF)
    || (code >= 0xFE20 && code <= 0xFE2F) || (code >= 0xFE00 && code <= 0xFE0F)
    || (code >= 0x1F3FB && code <= 0x1F3FF) || (code >= 0xE0020 && code <= 0xE007F)
    || (code >= 0xE0100 && code <= 0xE01EF) || code === 0x200D
}

// Code points with their UTF-16 offsets, so a cut never splits a surrogate pair.
function codePoints(text) {
  var out = []
  var value = String(text || "")
  for (var i = 0; i < value.length; i++) {
    var code = value.charCodeAt(i)
    var width = 1
    if (code >= 0xD800 && code <= 0xDBFF && i + 1 < value.length) {
      var low = value.charCodeAt(i + 1)
      if (low >= 0xDC00 && low <= 0xDFFF) {
        code = (code - 0xD800) * 0x400 + (low - 0xDC00) + 0x10000
        width = 2
      }
    }
    out.push({ code: code, start: i, end: i + width })
    i += width - 1
  }
  return out
}

function utf8Length(code) {
  if (code < 0x80) return 1
  if (code < 0x800) return 2
  if (code < 0x10000) return 3
  return 4
}

// Grapheme starts as UTF-16 offsets, plus the end of the text.
function graphemeBoundaries(text) {
  var points = codePoints(text)
  var starts = []
  var joined = false
  var pendingFlag = false
  for (var i = 0; i < points.length; i++) {
    var code = points[i].code
    if (code === 0x200D) { joined = true; continue }
    if (joined) { joined = false; continue }
    if (isExtender(code)) continue
    if (code >= 0x1F1E6 && code <= 0x1F1FF) {
      if (pendingFlag) { pendingFlag = false; continue }
      pendingFlag = true
    } else {
      pendingFlag = false
    }
    starts.push(points[i].start)
  }
  return starts
}

function graphemeCount(text) {
  return graphemeBoundaries(text).length
}

function byteLength(text) {
  var points = codePoints(text)
  var total = 0
  for (var i = 0; i < points.length; i++) total += utf8Length(points[i].code)
  return total
}

// The text cut to at most maxGraphemes graphemes and maxBytes bytes (0 = no
// byte limit), always on a grapheme boundary. A limit that is not a usable
// number leaves the text alone: an empty composer must never become one where
// nothing can be typed.
function limitText(text, maxGraphemes, maxBytes) {
  var value = String(text === undefined || text === null ? "" : text)
  var limit = Number(maxGraphemes)
  if (!isFinite(limit) || limit < 1) return value
  var starts = graphemeBoundaries(value)
  var cut = value.length
  if (starts.length > limit) cut = starts[Math.floor(limit)]
  var result = value.substring(0, cut)
  var bytes = Number(maxBytes)
  if (isFinite(bytes) && bytes > 0) {
    while (result !== "" && byteLength(result) > bytes) {
      var bounds = graphemeBoundaries(result)
      result = result.substring(0, bounds[bounds.length - 1])
    }
  }
  return result
}

// ------------------------------------------------------------ parsing

function parseJson(text) {
  try {
    return JSON.parse(String(text || ""))
  } catch (error) {
    return null
  }
}

// A feed page is { feed: [...], cursor }. Anything else is not a page, which
// the panel must tell apart from an empty one: only a real page may clear the
// "more to load" flag.
function feedPage(parsed) {
  if (!parsed || typeof parsed !== "object") return null
  var feed = parsed.feed
  if (!feed || typeof feed.length !== "number") return null
  var items = []
  for (var i = 0; i < feed.length; i++) {
    if (feed[i] && feed[i].post && feed[i].post.uri) items.push(feed[i])
  }
  return { items: items, cursor: String(parsed.cursor || "") }
}

// The same post can show up twice in a timeline, once on its own and once as
// someone's repost, so the key includes who reposted it.
function entryKey(item) {
  if (!item || !item.post) return ""
  var key = String(item.post.uri || "")
  if (item.reason && item.reason.by && item.reason.by.did) key += "|" + item.reason.by.did
  return key
}

function appendUnique(list, page) {
  var known = {}
  var merged = list && typeof list.length === "number" ? Array.prototype.slice.call(list) : []
  var i
  for (i = 0; i < merged.length; i++) known[entryKey(merged[i])] = true
  if (!page || typeof page.length !== "number") return merged
  for (i = 0; i < page.length; i++) {
    var key = entryKey(page[i])
    if (key === "" || known[key]) continue
    known[key] = true
    merged.push(page[i])
  }
  return merged
}

// The account that reposted this entry, or null for a plain post.
function reposter(item) {
  var reason = item && item.reason
  if (!reason || String(reason.$type || "").indexOf("reasonRepost") === -1) return null
  return reason.by || null
}

// Who the post answers, when the timeline says so. A parent that is blocked or
// gone has no author, and then there is nothing to say.
function replyParentAuthor(item) {
  var reply = item && item.reply
  if (!reply || !reply.parent || !reply.parent.author) return null
  return reply.parent.author
}

// Mentions come back from the helper with the notification reason attached.
function notificationLabel(item) {
  var reason = item && item.notificationReason
  if (reason === "reply") return "replied to you"
  if (reason === "quote") return "quoted you"
  if (reason === "mention") return "mentioned you"
  return ""
}

function displayName(author) {
  if (!author) return ""
  var name = String(author.displayName || "").trim()
  return name !== "" ? name : String(author.handle || "")
}

function handle(author) {
  if (!author || !author.handle) return ""
  var value = String(author.handle)
  // handle.invalid is what the AppView shows when a handle no longer resolves.
  return value === "handle.invalid" ? String(author.did || "") : "@" + value
}

// encodeURIComponent, but a DID keeps its colons: bsky.app expects
// /profile/did:plc:..., and every other character it could contain is already
// safe in a path.
function pathPart(value) {
  return encodeURIComponent(String(value)).replace(/%3A/gi, ":")
}

function rkeyOf(uri) {
  var parts = String(uri || "").split("/")
  return parts.length >= 5 ? parts[parts.length - 1] : ""
}

function profileUrl(author) {
  if (!author) return ""
  var actor = author.handle && author.handle !== "handle.invalid" ? author.handle : author.did
  return actor ? WEB_BASE + "/profile/" + pathPart(actor) : ""
}

// The post on bsky.app, for "open in browser" and for media the panel does not
// play itself (videos).
function postWebUrl(post) {
  if (!post || !post.author) return ""
  var rkey = rkeyOf(post.uri)
  var profile = profileUrl(post.author)
  return rkey && profile ? profile + "/post/" + pathPart(rkey) : ""
}

function isSensitive(post) {
  var labels = post && post.labels
  if (!labels || typeof labels.length !== "number") return false
  for (var i = 0; i < labels.length; i++) {
    var value = labels[i] && labels[i].val
    if (SENSITIVE_LABELS.indexOf(String(value)) !== -1) return true
  }
  return false
}

function count(value) {
  var n = Number(value)
  if (!isFinite(n) || n <= 0) return ""
  if (n >= 1000000) return (Math.floor(n / 100000) / 10) + "M"
  if (n >= 1000) return (Math.floor(n / 100) / 10) + "K"
  return String(Math.floor(n))
}

function formatTime(iso) {
  var date = new Date(iso)
  if (isNaN(date.getTime())) return ""
  var diff = Date.now() - date.getTime()
  var minutes = Math.floor(diff / 60000)
  if (minutes < 1) return "now"
  if (minutes < 60) return minutes + "m"
  var hours = Math.floor(minutes / 60)
  if (hours < 24) return hours + "h"
  var days = Math.floor(hours / 24)
  if (days < 7) return days + "d"
  return date.toLocaleDateString()
}

// Only http(s) is ever handed to Qt.openUrlExternally or an Image. Posts are
// written by strangers, so javascript:, file: and data: must never survive.
function safeHttpUrl(url) {
  var text = String(url || "").trim()
  return /^https?:\/\/[^\s]+$/i.test(text) ? text : ""
}

function escapeHtml(text) {
  return String(text || "")
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
}

// ------------------------------------------------------------ rich text
//
// A post is plain text plus facets that point at byte ranges of its UTF-8
// encoding. This maps those byte offsets back to string offsets, escapes every
// piece of text, and wraps only the faceted ranges in links. Nothing the author
// wrote is ever interpreted as markup.

function byteToStringOffsets(text) {
  var points = codePoints(text)
  var map = {}
  var bytes = 0
  for (var i = 0; i < points.length; i++) {
    map[bytes] = points[i].start
    bytes += utf8Length(points[i].code)
  }
  map[bytes] = String(text || "").length
  return map
}

function facetTarget(feature) {
  var type = String(feature && feature.$type || "")
  if (type.indexOf("#link") !== -1) return safeHttpUrl(feature.uri)
  if (type.indexOf("#mention") !== -1 && /^did:[a-z]+:[A-Za-z0-9._:%-]+$/.test(String(feature.did || "")))
    return WEB_BASE + "/profile/" + pathPart(feature.did)
  if (type.indexOf("#tag") !== -1 && feature.tag)
    return WEB_BASE + "/hashtag/" + pathPart(feature.tag)
  return ""
}

// The text with its facets resolved into ordered segments of { text, url }.
function segments(record) {
  var text = String(record && record.text || "")
  var facets = record && record.facets
  var list = []
  if (facets && typeof facets.length === "number") {
    var offsets = byteToStringOffsets(text)
    for (var i = 0; i < facets.length; i++) {
      var facet = facets[i]
      if (!facet || !facet.index || !facet.features || !facet.features.length) continue
      var start = offsets[facet.index.byteStart]
      var end = offsets[facet.index.byteEnd]
      // An index that does not land on a character boundary is malformed
      // (or meant for a different text); it is skipped, not guessed at.
      if (start === undefined || end === undefined || end <= start) continue
      var url = facetTarget(facet.features[0])
      if (url === "") continue
      list.push({ start: start, end: end, url: url })
    }
    list.sort(function (a, b) { return a.start - b.start })
  }
  var out = []
  var position = 0
  for (var j = 0; j < list.length; j++) {
    if (list[j].start < position) continue
    if (list[j].start > position) out.push({ text: text.substring(position, list[j].start), url: "" })
    out.push({ text: text.substring(list[j].start, list[j].end), url: list[j].url })
    position = list[j].end
  }
  if (position < text.length) out.push({ text: text.substring(position), url: "" })
  return out
}

// RichText ignores the item's color property and falls back to black, which is
// unreadable on a dark bar, so both colours are inlined.
function postRichText(record, baseColor, linkColor) {
  var parts = segments(record)
  var html = ""
  for (var i = 0; i < parts.length; i++) {
    var piece = escapeHtml(parts[i].text).replace(/\n/g, "<br>")
    if (parts[i].url !== "") {
      html += '<a href="' + escapeHtml(parts[i].url) + '"><font color="'
        + escapeHtml(linkColor || "#7aa2f7") + '">' + piece + "</font></a>"
    } else {
      html += piece
    }
  }
  return '<font color="' + escapeHtml(baseColor || "#ffffff") + '">' + html + "</font>"
}

function plainText(record) {
  return String(record && record.text || "")
}

// -------------------------------------------------------------- embeds
//
// A post view carries at most one embed: images, a gallery, a video, a link
// card, a quoted post, or a quoted post together with media. These unpack it
// into what a card renders, each from the single embed object so the card and
// its height never disagree about what is there.

function embedType(embed) {
  return String(embed && embed.$type || "")
}

function mediaEmbed(post) {
  var embed = post && post.embed
  var type = embedType(embed)
  if (type.indexOf("app.bsky.embed.recordWithMedia") === 0) return embed.media || null
  return embed || null
}

function pushImage(out, thumb, full, alt, video, link) {
  var url = safeHttpUrl(thumb) || safeHttpUrl(full)
  if (url === "" || out.length >= MAX_IMAGES) return
  out.push({ url: url, fullUrl: safeHttpUrl(full) || url, description: String(alt || ""),
    video: video === true, link: link || "" })
}

// Images, gallery items or a video thumbnail. A video is not played in the
// panel; its thumbnail opens the post on bsky.app.
function postMedia(post) {
  var out = []
  var embed = mediaEmbed(post)
  var type = embedType(embed)
  var i
  if (type.indexOf("app.bsky.embed.images") === 0) {
    var images = embed.images
    for (i = 0; images && i < images.length; i++)
      if (images[i]) pushImage(out, images[i].thumb, images[i].fullsize, images[i].alt, false)
  } else if (type.indexOf("app.bsky.embed.gallery") === 0) {
    var items = embed.items
    for (i = 0; items && i < items.length; i++)
      if (items[i]) pushImage(out, items[i].thumbnail, items[i].fullsize, items[i].alt, false)
  } else if (type.indexOf("app.bsky.embed.video") === 0) {
    pushImage(out, embed.thumbnail, "", embed.alt, true, postWebUrl(post))
  }
  return out
}

// A link card: title, domain and thumbnail of an external page.
function postExternal(post) {
  var embed = mediaEmbed(post)
  if (embedType(embed).indexOf("app.bsky.embed.external") !== 0 || !embed.external) return null
  var external = embed.external
  var url = safeHttpUrl(external.uri)
  if (url === "") return null
  var domain = /^https?:\/\/([^\/?#:]+)/i.exec(url)
  return {
    url: url,
    title: String(external.title || "").trim() || url,
    description: String(external.description || "").trim(),
    domain: domain ? domain[1].replace(/^www\./i, "") : "",
    image: safeHttpUrl(external.thumb)
  }
}

// A quoted post. Only a real post is shown; a quote of a list, a feed or a
// starter pack, or of a post that is gone or blocked, becomes a one-line note.
function postQuote(post) {
  var embed = post && post.embed
  var type = embedType(embed)
  var view = null
  if (type.indexOf("app.bsky.embed.recordWithMedia") === 0) view = embed.record && embed.record.record
  else if (type.indexOf("app.bsky.embed.record") === 0) view = embed.record
  if (!view) return null
  var viewType = embedType(view)
  if (viewType.indexOf("#viewRecord") !== -1 && view.author) {
    var quoted = { uri: view.uri, author: view.author }
    return {
      author: view.author,
      text: String(view.value && view.value.text || ""),
      createdAt: String(view.value && view.value.createdAt || view.indexedAt || ""),
      url: postWebUrl(quoted),
      note: ""
    }
  }
  var note = "Quoted content"
  if (viewType.indexOf("#viewNotFound") !== -1) note = "Quoted post was deleted"
  else if (viewType.indexOf("#viewBlocked") !== -1) note = "Quoted post is blocked"
  else if (viewType.indexOf("#viewDetached") !== -1) note = "Quote removed by its author"
  else if (String(view.name || view.displayName || "") !== "")
    note = String(view.name || view.displayName)
  return { author: null, text: "", createdAt: "", url: "", note: note }
}

// --------------------------------------------------------- updating posts
//
// A like, repost or bookmark is applied to the posts already on screen instead
// of reloading three feeds. These return a new list, which is what makes a QML
// var property notify.

function copyPost(post, viewerPatch, counts) {
  var next = {}
  var key
  for (key in post) next[key] = post[key]
  var viewer = {}
  for (key in (post.viewer || {})) viewer[key] = post.viewer[key]
  for (key in viewerPatch) viewer[key] = viewerPatch[key]
  next.viewer = viewer
  for (key in counts) next[key] = Math.max(0, Number(post[key] || 0) + counts[key])
  return next
}

function patchPost(list, uri, viewerPatch, counts) {
  var out = []
  if (!list || typeof list.length !== "number") return out
  for (var i = 0; i < list.length; i++) {
    var item = list[i]
    if (item && item.post && item.post.uri === uri) {
      var next = {}
      for (var key in item) next[key] = item[key]
      next.post = copyPost(item.post, viewerPatch || {}, counts || {})
      out.push(next)
    } else {
      out.push(item)
    }
  }
  return out
}

// Following is a property of the author, so every post by them changes.
function patchAuthorFollow(list, did, followUri) {
  var out = []
  if (!list || typeof list.length !== "number") return out
  for (var i = 0; i < list.length; i++) {
    var item = list[i]
    if (item && item.post && item.post.author && item.post.author.did === did) {
      var author = {}
      var key
      for (key in item.post.author) author[key] = item.post.author[key]
      var viewer = {}
      for (key in (author.viewer || {})) viewer[key] = author.viewer[key]
      viewer.following = followUri || undefined
      author.viewer = viewer
      var post = {}
      for (key in item.post) post[key] = item.post[key]
      post.author = author
      var next = {}
      for (key in item) next[key] = item[key]
      next.post = post
      out.push(next)
    } else {
      out.push(item)
    }
  }
  return out
}

function isFollowing(author) {
  return !!(author && author.viewer && author.viewer.following)
}

// ------------------------------------------------------------------ auth
//
// The panel's only view of the credentials. The tokens are owned by
// bluesky_helper.py and never cross back into QML.

function emptyAuth() {
  return { service: "", handle: "", did: "", hasSession: false }
}

function decodeAuth(data) {
  if (!data || typeof data !== "object") return emptyAuth()
  // Field by field, so a token slipped into the input cannot ride along.
  return {
    service: String(data.service || ""),
    handle: String(data.handle || ""),
    did: String(data.did || ""),
    hasSession: data.hasSession === true
  }
}

function isAuthed(auth) {
  return !!(auth && auth.did && auth.hasSession === true)
}

function decode(data) {
  if (!data || typeof data !== "object") return { auth: emptyAuth() }
  return { auth: decodeAuth(data.auth) }
}

// The helper's error names, turned into something a person can act on.
function errorName(stderrText) {
  var match = /BLUESKY_ERROR:([^\s]+)/.exec(String(stderrText || ""))
  return match ? match[1] : ""
}

function loginErrorMessage(name, service) {
  var server = displayService(service)
  if (name === "invalid_credentials") return "Wrong handle or app password"
  if (name === "auth_factor_required")
    return "This account uses two-factor login. Create an app password in Bluesky under Settings → Privacy and security → App passwords"
  if (name === "account_takedown") return "This account has been taken down"
  if (name === "network_error") return "Could not reach " + server
  if (name === "insecure_service") return "Only https servers are supported"
  if (name === "bad_identifier") return "Enter your handle (e.g. alice.bsky.social)"
  return "Login failed" + (name ? " (" + name + ")" : "")
}

function uploadErrorMessage(name, file) {
  var prefix = "Upload failed: " + baseName(file)
  if (name === "image_too_large") return prefix + " is over 2 MB (install ImageMagick to have it shrunk)"
  if (name === "file_too_large") return prefix + " is too large"
  if (name === "unsupported_image") return prefix + " is not a JPEG, PNG, GIF or WebP"
  if (name === "not_a_regular_file" || name === "unreadable_file") return prefix + " cannot be read"
  return prefix
}

function postErrorMessage(name) {
  if (name === "post_too_long") return "The post is too long"
  if (name === "empty_post") return "Nothing to post"
  if (name === "network_error") return "Could not reach the server"
  return "Failed to post"
}

function displayService(service) {
  var text = String(service || "").trim()
  if (text === "") text = DEFAULT_SERVICE
  return text.replace(/^https?:\/\//i, "").replace(/\/+$/, "")
}

if (typeof module !== "undefined") {
  module.exports = {
    DEFAULT_SERVICE: DEFAULT_SERVICE,
    PAGE_SIZE: PAGE_SIZE,
    MAX_POST_GRAPHEMES: MAX_POST_GRAPHEMES,
    MAX_POST_BYTES: MAX_POST_BYTES,
    MAX_IMAGES: MAX_IMAGES,
    MAX_ALT_TEXT: MAX_ALT_TEXT,
    SENSITIVE_LABELS: SENSITIVE_LABELS,
    isLoopbackHost: isLoopbackHost,
    normalizeService: normalizeService,
    normalizeIdentifier: normalizeIdentifier,
    displayService: displayService,
    loadCmd: loadCmd,
    loginCmd: loginCmd,
    logoutCmd: logoutCmd,
    timelineCmd: timelineCmd,
    discoverCmd: discoverCmd,
    mentionsCmd: mentionsCmd,
    likeCmd: likeCmd,
    unlikeCmd: unlikeCmd,
    repostCmd: repostCmd,
    unrepostCmd: unrepostCmd,
    followCmd: followCmd,
    unfollowCmd: unfollowCmd,
    bookmarkCmd: bookmarkCmd,
    unbookmarkCmd: unbookmarkCmd,
    uploadMediaCmd: uploadMediaCmd,
    postCmd: postCmd,
    pickMediaCmd: pickMediaCmd,
    replyRefFor: replyRefFor,
    uploadedImages: uploadedImages,
    postSpec: postSpec,
    baseName: baseName,
    fileUrl: fileUrl,
    graphemeCount: graphemeCount,
    byteLength: byteLength,
    limitText: limitText,
    parseJson: parseJson,
    feedPage: feedPage,
    entryKey: entryKey,
    appendUnique: appendUnique,
    reposter: reposter,
    replyParentAuthor: replyParentAuthor,
    notificationLabel: notificationLabel,
    displayName: displayName,
    handle: handle,
    profileUrl: profileUrl,
    postWebUrl: postWebUrl,
    isSensitive: isSensitive,
    count: count,
    formatTime: formatTime,
    safeHttpUrl: safeHttpUrl,
    escapeHtml: escapeHtml,
    segments: segments,
    postRichText: postRichText,
    plainText: plainText,
    postMedia: postMedia,
    postExternal: postExternal,
    postQuote: postQuote,
    patchPost: patchPost,
    patchAuthorFollow: patchAuthorFollow,
    isFollowing: isFollowing,
    emptyAuth: emptyAuth,
    decodeAuth: decodeAuth,
    isAuthed: isAuthed,
    decode: decode,
    errorName: errorName,
    loginErrorMessage: loginErrorMessage,
    uploadErrorMessage: uploadErrorMessage,
    postErrorMessage: postErrorMessage
  }
}
