# Bluesky client (saigkill.bluesky)

Bluesky client for the Omarchy bar: app password login, three feeds, a composer
with images and alt text, and the usual interactions. Built after the
[Mastodon client](https://github.com/saigkill/omarchy-mastodon) and sharing its
design rules.

![Preview](https://github.com/saigkill/omarchy-bluesky/blob/main/preview.png?raw=true)

Features

- **Bar widget**: a butterfly in the bar, red while logged out. Opens a panel
  with:
  - **Login with an app password**: handle, app password and, optionally, your
    own PDS. The password is exchanged for a session at once and never stored
  - **Three tabs**: *Following* (your timeline), *Discover* (Bluesky's own
    Discover feed) and *Mentions* (mentions, replies and quotes)
  - **Composer**: one box in every tab, counted in graphemes against Bluesky's
    300-per-post limit
  - **Links, mentions and hashtags** in what you write become real facets, so
    they are clickable for everyone. A mention only links when the handle
    resolves; an unknown one stays plain text
  - **Link cards**: a post with a link and no images gets a preview card (title,
    description, picture) built from the page's OpenGraph tags
  - **Images**: up to four per post, chosen with Omarchy's file chooser and
    uploaded right away. With ImageMagick installed they are scaled to 2000 px,
    stripped of EXIF (GPS!) and compressed under Bluesky's 2 MB limit
  - **Alt text per image**: click a thumbnail. The badge reads `+ALT` until it
    has one
  - **Replies** that thread correctly (root and parent), **Repost**, **Like**,
    **Bookmark**, **Follow**, and **Open on bsky.app**, all applied to the posts on
    screen without reloading the feed
  - **Rich feed**: images, galleries, video thumbnails (open on bsky.app), link
    cards, quoted posts, "X reposted", "Replying to @y", counts
  - **Content labels**: media on posts labelled adult or graphic stays behind
    "Show media"
  - **Infinite scroll**: the end of a feed loads the next page

While a text field has focus, the panel's `j`/`k`/`h`/`l`/space shortcuts go to
the field instead of the feed.

## Logging in

1. In Bluesky, go to **Settings → Privacy and security → App passwords** and
   create one (e.g. "Omarchy")
2. Open the panel, enter your handle (`alice.bsky.social`, with or without `@`)
   and the app password
3. Leave the server empty for a bsky.social account, or enter your own PDS
   (`pds.example.org`)

Your main account password works too, unless you have two-factor login enabled;
then Bluesky insists on an app password and the panel says so. An app password
is the better choice either way: it can be revoked on its own and cannot change
your account settings.

**Why not OAuth, as in the Mastodon client?** Bluesky's OAuth requires DPoP:
every request is signed with an ES256 key. Python's standard library has no
ECDSA, so OAuth would mean either a dependency (`python-cryptography`) or
hand-written cryptography. App passwords are what Bluesky offers third-party
clients for exactly this case.

## Credential handling

All Bluesky API calls and all reads and writes of the state file go through
`bluesky_helper.py`. The rules are the Mastodon client's:

- **Nothing secret ever appears on a command line.** `/proc/<pid>/cmdline` is
  world readable. The app password travels to the helper in
  `BLUESKY_APP_PASSWORD` on the environment (`/proc/<pid>/environ` is readable
  by the owner only), is cleared from the `Process` again when the helper
  exits, and is removed from the text field the moment login starts.
- **The panel holds no token.** Its view of the session is
  `{ service, handle, did, hasSession }`. The access and refresh tokens never
  leave the helper.
- **One subcommand per action.** The helper has no generic "fetch this URL".
  What a caller can do with the session is exactly `timeline`, `discover`,
  `mentions`, `like`, `unlike`, `repost`, `unrepost`, `follow`, `unfollow`,
  `bookmark`, `unbookmark`, `upload`, `post`, plus `login`, `logout` and `load`.
  Every URI, CID and DID a caller passes is validated before anything is sent,
  and `unlike`/`unrepost`/`unfollow` only delete records **in your own repo, of
  that kind**: `unlike` cannot be talked into deleting a post.
- **`auth.json` is `0600`, written atomically and never through a symlink.**
  A symlink planted at the path is refused for reading and writing.
- **https only.** The server you log in to, and the PDS from your DID document,
  must be `https` (plain `http` only to loopback, for a local development PDS).
  Userinfo tricks (`bsky.social@evil.example`) are refused, and a redirect that
  changes origin is not followed, so the token only ever goes to the server
  that issued it.
- **Sessions refresh themselves.** The access token lives about two hours. The
  helper refreshes it a minute before it runs out, or when the server calls it
  expired, under a file lock so that several helpers running at once rotate
  the refresh token only once. If the refresh token itself is refused, the
  session ends, the panel shows the login form with your handle filled in, and
  only the app password has to be typed again.
- **Response bodies are capped at 10 MiB.**

## Images

The **Image** button opens `omarchy-file-select`, the desktop's own chooser. The
panel closes while it is open, because it lives in the overlay layer and the
chooser would open underneath it; text, reply target and attached images survive
the round trip.

Each picked file is uploaded right away (`com.atproto.repo.uploadBlob`), one at a
time, and the post later points at the blob the server returned. The thumbnail
in the composer is the local file. The file path travels in `BLUESKY_UPLOAD_PATH`,
never on the command line.

Before anything is read, the helper refuses what is not a regular file (a fifo
would block it), not `.jpg/.jpeg/.png/.gif/.webp`, or over 40 MB. Then:

- **With ImageMagick** (`magick`, installed on Omarchy): the image is re-encoded
  once with resource limits and the input format named explicitly. It is
  auto-rotated, stripped of metadata and scaled to at most 2000 px. PNGs stay
  PNG if they fit; everything else becomes JPEG, with the quality stepping down
  until it is under 2 MB.
- **Without it**: the file is uploaded as is if it is under 2 MB, and refused
  otherwise.

The alt text belongs to the post record on Bluesky, not to the upload, so it is
kept in the composer and sent with the post. That also means it can be written
while the upload is still running.

## Link cards

Bluesky does not build link previews on the server; the client that writes the
post does. When a post has a link and no images, the helper fetches that page
(at most 1 MB, 8 seconds), reads its OpenGraph title, description and picture,
uploads the picture as a blob and attaches an `app.bsky.embed.external` card. The
linked site therefore sees one request from your machine, without any
credential. A page that is slow or has no title simply gets no card; the post is
sent either way.

## Panel layout

```
┌──────────────────────────────┐
│ BLUESKY @you        ⟳  ⏻     │  header        stays put
├──────────────────────────────┤
│ [Following] [Discover] [Ment…│  tabs          stays put
├──────────────────────────────┤
│ What's up?                   │  composer      stays put
│ [ Post ] [ Image ]    23/300 │
│ ▢ALT ▢ALT                    │  thumbnails and alt text
├──────────────────────────────┤
│ ⇄ X reposted                 │  ┐
│ Name @handle · 3h   [Follow] │  │ the feed scrolls,
│ post text … #tag @mention    │  │ only this part moves
│ ▢ ▢   [link card] [quote]    │  │
│ ↩ 12  ⇄ 4  ♥ 98  🔖  ↗        │  ┘
└──────────────────────────────┘
```

To change the panel height, edit `Style.space(700)` in `Panel.qml`.

## API endpoints used

- `com.atproto.server.createSession` / `refreshSession` / `deleteSession`: the session
- `app.bsky.feed.getTimeline`: Following
- `app.bsky.feed.getFeed` with `at://did:plc:z72i7hdynmk6r22z27h6tvur/app.bsky.feed.generator/whats-hot`: Discover
- `app.bsky.notification.listNotifications?reasons=mention,reply,quote` + `app.bsky.feed.getPosts`: Mentions
- `com.atproto.repo.createRecord` / `deleteRecord`: post, like, repost, follow
- `com.atproto.repo.uploadBlob`: images and link card pictures
- `com.atproto.identity.resolveHandle`: mentions in what you write
- `app.bsky.bookmark.createBookmark` / `deleteBookmark`: bookmarks

Requests go to your PDS, which forwards the `app.bsky.*` ones to Bluesky's
AppView.

## Files and where state lives

| Path | What it is |
| --- | --- |
| `Panel.qml` | Panel UI: login, tabs, composer, feed, cards |
| `BarWidget.qml` | Bar slot, icon button, login orchestration |
| `Model.js` | Pure helpers: command builders, feed and embed parsing, rich text from facets, grapheme counting. Holds no credential |
| `bluesky_helper.py` | Owns every credential: talks to the PDS, reads/writes the state file, builds post records, facets, link cards and images |
| `manifest.json` | Omarchy plugin manifest |
| `tests/` | `test_model.js`, `test_helper.py`, and real API answers in `fixtures/` |
| `~/.local/state/omarchy-bluesky/auth.json` | Session tokens, `0600`, owned by `bluesky_helper.py` (**yours, never in the repo**) |

Do not put anything in the plugin directory that changes at runtime. Quickshell
watches it and reloads the plugin, which closes an open panel.

## Install

The plugin id is `saigkill.bluesky`.

### From git

```sh
omarchy plugin add https://github.com/saigkill/omarchy-bluesky.git --enable
omarchy restart shell
```

### From a local clone (development)

```sh
git clone https://github.com/saigkill/omarchy-bluesky.git ~/src/omarchy-bluesky
chmod +x ~/src/omarchy-bluesky/bluesky_helper.py
ln -s ~/src/omarchy-bluesky ~/.config/omarchy/plugins/saigkill.bluesky
omarchy plugin enable saigkill.bluesky
omarchy restart shell
```

With the symlink, edits are picked up on the next `omarchy restart shell`. Use a
full restart rather than the automatic reload: QML caches components it has
already loaded.

## Remove

```sh
omarchy plugin remove saigkill.bluesky --yes
```

The session in `~/.local/state/omarchy-bluesky/` is not removed with the plugin.
Use the logout button in the panel (which also revokes the session on the
server), delete that directory, or revoke the app password in Bluesky under
**Settings → Privacy and security → App passwords**.

## Troubleshooting

**"Wrong handle or app password".** Check the handle (`alice.bsky.social`, not
the display name). For a self-hosted account, enter your PDS as the server.

**"Your session has expired".** The refresh token ran out or was revoked (for
example by deleting the app password). Log in again.

**"Upload failed: … is over 2 MB".** ImageMagick is missing; install it
(`sudo pacman -S imagemagick`) or pick a smaller picture.

**Nothing happens / panel empty.** Check the log:

```sh
journalctl --user --since "-2min" | grep -i bluesky
```

`qmllint` is not a reliable check for this plugin: the `qs.*` imports make it
report false positives. If the log shows a `Syntax error`, count the braces as
described in `AGENTS.md`.

## Testing

```sh
node tests/test_model.js                            # Model.js
python3 -m unittest discover -s tests -v            # bluesky_helper.py
```

The helper tests run against a local TLS server that plays a PDS: login, token
refresh (including four helpers refreshing at once), session expiry, symlink and
permission handling of the state file, redirect and size limits, record
validation, facets with UTF-8 byte offsets, uploads with and without ImageMagick.
The model tests run the feed parsing against real answers from the Bluesky API in
`tests/fixtures/`.

## License

[MIT](LICENSE). Copyright (c) 2026 Sascha Manns.
