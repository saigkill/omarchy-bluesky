import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Model.js" as Model

Panel {
  id: root
  moduleName: "saigkill.bluesky"
  ipcTarget: "saigkill.bluesky"
  manageIpc: false

  property var anchorItem: null
  property var hostWidget: null

  readonly property var auth: hostWidget ? hostWidget.auth : Model.emptyAuth()
  readonly property bool authed: hostWidget ? hostWidget.authed : false
  // bluesky_helper.py owns the session tokens; they never travel back into the
  // panel, so there is no token property here to leak.
  readonly property string helperScript: hostWidget ? hostWidget.helperScript
    : Qt.resolvedUrl("bluesky_helper.py").toString().replace("file://", "")

  property string loginService: ""
  property string loginIdentifier: ""
  readonly property bool loggingIn: hostWidget ? hostWidget.loginPending : false
  readonly property string loginError: hostWidget ? hostWidget.loginError : ""

  // After an expired session the login form comes back with the handle the
  // user had, so only the app password has to be typed again.
  onAuthChanged: {
    if (root.loginIdentifier === "" && root.auth.handle) root.loginIdentifier = root.auth.handle
  }

  // ------------------------------------------------------------ feeds
  //
  // Bluesky pages with an opaque cursor rather than an id, and an absent cursor
  // means the end of the feed.
  property int currentTab: 0
  readonly property var tabNames: ["Following", "Discover", "Mentions"]
  property var followingFeed: []
  property var discoverFeed: []
  property var mentionsFeed: []
  property string followingCursor: ""
  property string discoverCursor: ""
  property string mentionsCursor: ""
  property bool followingHasMore: true
  property bool discoverHasMore: true
  property bool mentionsHasMore: true
  property bool loadingMore: false
  property string feedError: ""
  property bool loadingFeed: false

  readonly property var currentFeed: currentTab === 0 ? followingFeed
    : (currentTab === 1 ? discoverFeed : mentionsFeed)
  readonly property bool hasMore: currentTab === 0 ? followingHasMore
    : (currentTab === 1 ? discoverHasMore : mentionsHasMore)
  readonly property bool feedEmpty: currentFeed.length === 0

  // ------------------------------------------------------------ composer
  property string composerText: ""
  // { root: {uri, cid}, parent: {uri, cid} } while replying, otherwise null.
  property var replyRef: null
  property string replyToUser: ""
  property bool posting: false
  property string postError: ""

  // Images attached to the composer, each
  // { path, name, blob, aspectRatio, failed, description }. A file is uploaded
  // the moment it is attached, and the blob reference the server hands back is
  // what the post later points at. The alt text belongs to the post record on
  // Bluesky, not to the upload, so it is kept here and sent with the post.
  property var media: []
  // One Process runs one command at a time, so a multi-file selection is
  // uploaded one after the other from this queue.
  property var mediaQueue: []
  property bool pickingMedia: false
  property bool uploadingMedia: false
  property string uploadingPath: ""
  property string altPath: ""
  property string altDraft: ""
  readonly property bool busyWithMedia: root.pickingMedia || root.uploadingMedia
    || root.mediaQueue.length > 0
  readonly property int mediaCount: Model.uploadedImages(root.media).length
  readonly property bool canPost: root.composerText.trim() !== "" || root.mediaCount > 0
  readonly property int composerLength: Model.graphemeCount(root.composerText)

  // ------------------------------------------------------------ actions
  //
  // Likes, reposts, bookmarks and follows run through one Process, queued, and
  // are applied to the posts already on screen instead of reloading three
  // feeds. `pending` holds a key per action in flight, so a double click does
  // not like a post twice.
  property var actionQueue: []
  property var currentAction: null
  property var pending: ({})

  readonly property color contentForeground: bar ? bar.barForeground : Color.foreground
  readonly property string contentFontFamily: bar ? bar.fontFamily : Style.font.family

  // The composer text lives on the panel and the field is kept in step through
  // this one function. A `text: root.composerText` binding would be dropped for
  // good by the first imperative write (cutting an over-long text), and it
  // cannot be put back from inside a signal handler.
  function setComposerText(value) {
    root.composerText = value
    if (composerInput && composerInput.text !== value) composerInput.text = value
  }

  function switchPanel(direction) {
    if (root.bar && typeof root.bar.switchPanelFrom === "function")
      return root.bar.switchPanelFrom(root.hostWidget || root, direction)
    return false
  }

  function login() {
    if (!root.hostWidget || root.loggingIn) return
    var password = passwordField.text
    // The password is handed on and dropped from the field at once, so the
    // panel holds no copy of it while the login runs.
    passwordField.text = ""
    root.hostWidget.startLogin(root.loginService, root.loginIdentifier, password)
  }

  function onLoginSuccess() {
    root.loadFeeds()
  }

  // The helper exits 7 when the refresh token itself was refused. It has
  // already ended the session, so the panel only has to forget what it shows.
  function checkSession(exitCode) {
    if (exitCode !== 7) return false
    root.clearState()
    if (root.hostWidget) root.hostWidget.sessionExpired()
    return true
  }

  // The panel object outlives the login that filled it and a shell restart
  // rebuilds it empty, so an empty feed is filled on the first open; a feed
  // that has entries is left alone rather than reloaded on every open.
  onOpenedChanged: {
    if (!root.opened || !root.authed) return
    if (root.loadingFeed || !root.feedEmpty) return
    root.loadFeeds()
  }

  // ------------------------------------------------------------ loading

  function loadFeeds() {
    if (!root.authed) return
    root.loadingFeed = true
    root.feedError = ""
    followingProc.command = Model.timelineCmd(root.helperScript)
    followingProc.running = true
  }

  // A page that does not parse is a failed request, not an empty feed: only a
  // real page may say there is nothing more to load.
  function takePage(exitCode, text, listProperty, cursorProperty, moreProperty, append) {
    var page = Model.feedPage(Model.parseJson(text))
    if (exitCode !== 0 || page === null) return false
    root[listProperty] = append ? Model.appendUnique(root[listProperty], page.items) : page.items
    root[cursorProperty] = page.cursor
    root[moreProperty] = page.cursor !== ""
    return true
  }

  function onFollowingExited(exitCode) {
    if (root.checkSession(exitCode)) return
    if (!root.takePage(exitCode, followingOut.text, "followingFeed", "followingCursor",
        "followingHasMore", false))
      root.feedError = "Failed to load the Following feed"
    discoverProc.command = Model.discoverCmd(root.helperScript)
    discoverProc.running = true
  }

  function onDiscoverExited(exitCode) {
    if (root.checkSession(exitCode)) return
    root.takePage(exitCode, discoverOut.text, "discoverFeed", "discoverCursor",
      "discoverHasMore", false)
    mentionsProc.command = Model.mentionsCmd(root.helperScript)
    mentionsProc.running = true
  }

  function onMentionsExited(exitCode) {
    if (root.checkSession(exitCode)) return
    root.takePage(exitCode, mentionsOut.text, "mentionsFeed", "mentionsCursor",
      "mentionsHasMore", false)
    root.loadingFeed = false
  }

  // Called whenever the feed moves. Reaching its tail pulls the next page in;
  // the guard in loadMore keeps a fast scroll from queueing several requests.
  function maybeLoadMore() {
    if (!root.authed || root.loadingFeed || root.loadingMore) return
    if (!root.hasMore || root.feedEmpty) return
    var remaining = scroll.contentHeight - (scroll.contentY + scroll.height)
    if (remaining > Style.space(320)) return
    root.loadMore()
  }

  function loadMore() {
    if (!root.authed || root.loadingMore || !root.hasMore) return
    root.loadingMore = true
    root.feedError = ""
    if (root.currentTab === 0) {
      moreProc.command = Model.timelineCmd(root.helperScript, root.followingCursor)
    } else if (root.currentTab === 1) {
      moreProc.command = Model.discoverCmd(root.helperScript, root.discoverCursor)
    } else {
      moreProc.command = Model.mentionsCmd(root.helperScript, root.mentionsCursor)
    }
    moreProc.tab = root.currentTab
    moreProc.running = true
  }

  function onMoreExited(exitCode) {
    root.loadingMore = false
    if (root.checkSession(exitCode)) return
    var names = [
      ["followingFeed", "followingCursor", "followingHasMore"],
      ["discoverFeed", "discoverCursor", "discoverHasMore"],
      ["mentionsFeed", "mentionsCursor", "mentionsHasMore"]
    ][moreProc.tab]
    if (!root.takePage(exitCode, moreOut.text, names[0], names[1], names[2], true))
      root.feedError = "Could not load older posts"
  }

  // ------------------------------------------------------------ actions

  function actionKey(kind, id) {
    var family = kind.replace(/^un/, "")
    return family + "|" + id
  }

  function isPending(kind, id) {
    return root.pending[root.actionKey(kind, id)] === true
  }

  function setPending(key, value) {
    var next = {}
    for (var k in root.pending) next[k] = root.pending[k]
    if (value) next[key] = true
    else delete next[key]
    root.pending = next
  }

  function queueAction(action) {
    var key = root.actionKey(action.kind, action.id)
    if (root.pending[key]) return
    action.key = key
    root.setPending(key, true)
    root.actionQueue = root.actionQueue.concat([action])
    root.runNextAction()
  }

  function runNextAction() {
    if (actionProc.running || root.actionQueue.length === 0) return
    var action = root.actionQueue[0]
    root.actionQueue = root.actionQueue.slice(1)
    root.currentAction = action
    actionProc.command = action.cmd
    actionProc.running = true
  }

  function patchAll(update) {
    root.followingFeed = update(root.followingFeed)
    root.discoverFeed = update(root.discoverFeed)
    root.mentionsFeed = update(root.mentionsFeed)
  }

  function onActionExited(exitCode) {
    var action = root.currentAction
    root.currentAction = null
    if (action) root.setPending(action.key, false)
    if (root.checkSession(exitCode)) return
    var result = Model.parseJson(actionOut.text)
    if (action && exitCode === 0 && result) {
      var uri = action.id
      var created = result.uri ? String(result.uri) : ""
      if (action.kind === "like" && created !== "")
        root.patchAll(function (list) { return Model.patchPost(list, uri, { like: created }, { likeCount: 1 }) })
      else if (action.kind === "unlike")
        root.patchAll(function (list) { return Model.patchPost(list, uri, { like: undefined }, { likeCount: -1 }) })
      else if (action.kind === "repost" && created !== "")
        root.patchAll(function (list) { return Model.patchPost(list, uri, { repost: created }, { repostCount: 1 }) })
      else if (action.kind === "unrepost")
        root.patchAll(function (list) { return Model.patchPost(list, uri, { repost: undefined }, { repostCount: -1 }) })
      else if (action.kind === "bookmark")
        root.patchAll(function (list) { return Model.patchPost(list, uri, { bookmarked: true }, { bookmarkCount: 1 }) })
      else if (action.kind === "unbookmark")
        root.patchAll(function (list) { return Model.patchPost(list, uri, { bookmarked: false }, { bookmarkCount: -1 }) })
      else if (action.kind === "follow" && created !== "")
        root.patchAll(function (list) { return Model.patchAuthorFollow(list, uri, created) })
    } else if (action) {
      root.feedError = "That did not work, please try again"
    }
    root.runNextAction()
  }

  function toggleLike(post) {
    var viewer = post.viewer || {}
    if (viewer.like) root.queueAction({ kind: "unlike", id: post.uri, cmd: Model.unlikeCmd(root.helperScript, viewer.like) })
    else root.queueAction({ kind: "like", id: post.uri, cmd: Model.likeCmd(root.helperScript, post.uri, post.cid) })
  }

  function toggleRepost(post) {
    var viewer = post.viewer || {}
    if (viewer.repost) root.queueAction({ kind: "unrepost", id: post.uri, cmd: Model.unrepostCmd(root.helperScript, viewer.repost) })
    else root.queueAction({ kind: "repost", id: post.uri, cmd: Model.repostCmd(root.helperScript, post.uri, post.cid) })
  }

  function toggleBookmark(post) {
    var viewer = post.viewer || {}
    if (viewer.bookmarked) root.queueAction({ kind: "unbookmark", id: post.uri, cmd: Model.unbookmarkCmd(root.helperScript, post.uri) })
    else root.queueAction({ kind: "bookmark", id: post.uri, cmd: Model.bookmarkCmd(root.helperScript, post.uri, post.cid) })
  }

  function follow(author) {
    if (!author || !author.did || Model.isFollowing(author)) return
    root.queueAction({ kind: "follow", id: author.did, cmd: Model.followCmd(root.helperScript, author.did) })
  }

  function openExternal(url) {
    var safe = Model.safeHttpUrl(url)
    if (safe !== "") Qt.openUrlExternally(safe)
  }

  // ------------------------------------------------------------ replying

  function startReply(post) {
    var ref = Model.replyRefFor(post)
    if (ref === null) return
    root.replyRef = ref
    root.replyToUser = Model.handle(post.author)
    root.setComposerText("")
    Qt.callLater(root.focusComposer)
  }

  // The composer sits at the top of the panel, so a reply target further down
  // the feed has to scroll back to it and take the keyboard.
  function focusComposer() {
    scroll.contentY = 0
    if (composerInput) composerInput.forceActiveFocus()
  }

  function cancelReply() {
    root.replyRef = null
    root.replyToUser = ""
    root.setComposerText("")
    if (keyCatcher) keyCatcher.forceActiveFocus()
  }

  // ------------------------------------------------------------ images

  // The panel closes while the chooser is up: it lives in the Overlay layer, so
  // the chooser, an ordinary window, would open underneath it. The panel object
  // stays alive meanwhile, so the text, the reply target and the images already
  // attached all survive.
  function attachImages() {
    if (root.busyWithMedia || !root.authed) return
    root.pickingMedia = true
    root.postError = ""
    root.close()
    pickMediaProc.command = Model.pickMediaCmd()
    pickMediaProc.running = true
  }

  function onPickMediaExited(exitCode) {
    root.pickingMedia = false
    // omarchy-file-select exits 1 when closed without a selection and 2 when
    // it never opened; neither is worth reporting. One path per line.
    var lines = String(pickMediaOut.text || "").split("\n")
    var picked = []
    for (var i = 0; i < lines.length; i++) {
      var path = lines[i].trim()
      if (path === "") continue
      if (root.media.length + root.mediaQueue.length + picked.length >= Model.MAX_IMAGES) {
        root.postError = "At most " + Model.MAX_IMAGES + " images per post"
        break
      }
      picked.push(path)
    }
    // Assigned rather than pushed into: an in-place mutation of a var array
    // does not notify, and busyWithMedia is bound to the queue's length.
    root.mediaQueue = root.mediaQueue.concat(picked)
    root.open()
    root.startNextUpload()
    Qt.callLater(root.focusComposer)
  }

  function startNextUpload() {
    if (root.uploadingMedia || root.mediaQueue.length === 0) return
    var path = root.mediaQueue[0]
    root.mediaQueue = root.mediaQueue.slice(1)
    root.uploadingPath = path
    root.uploadingMedia = true
    root.media = root.media.concat([{
      path: path, name: Model.baseName(path), blob: null, aspectRatio: null,
      failed: false, description: ""
    }])
    uploadMediaProc.command = Model.uploadMediaCmd(root.helperScript)
    uploadMediaProc.environment = ({ BLUESKY_UPLOAD_PATH: path })
    uploadMediaProc.running = true
  }

  function onUploadMediaExited(exitCode) {
    root.uploadingMedia = false
    var path = root.uploadingPath
    root.uploadingPath = ""
    if (root.checkSession(exitCode)) return
    var parsed = Model.parseJson(uploadMediaOut.text)
    var blob = exitCode === 0 && parsed && parsed.blob ? parsed.blob : null
    if (blob === null)
      root.postError = Model.uploadErrorMessage(Model.errorName(uploadMediaErr.text), path)
    var next = []
    for (var i = 0; i < root.media.length; i++) {
      var entry = root.media[i]
      if (entry.path === path) {
        // The description is carried over: it may have been typed while the
        // upload was still running.
        entry = { path: entry.path, name: entry.name, blob: blob,
          aspectRatio: blob && parsed.aspectRatio ? parsed.aspectRatio : null,
          failed: blob === null, description: entry.description || "" }
      }
      next.push(entry)
    }
    root.media = next
    root.startNextUpload()
  }

  function removeMedia(entry) {
    if (root.altPath === entry.path) root.closeAltText()
    var next = []
    for (var i = 0; i < root.media.length; i++) {
      if (root.media[i].path !== entry.path) next.push(root.media[i])
    }
    root.media = next
  }

  // ------------------------------------------------------------ alt texts
  //
  // On Bluesky the alt text is part of the post, so saving it is a local edit
  // that needs no request and can happen before the upload has even finished.

  function openAltText(entry) {
    root.altPath = entry.path
    root.altDraft = entry.description || ""
    altField.text = root.altDraft
    Qt.callLater(function () { altField.forceActiveFocus() })
  }

  function closeAltText() {
    root.altPath = ""
    root.altDraft = ""
  }

  function saveAltText() {
    if (root.altPath === "") return
    var next = []
    for (var i = 0; i < root.media.length; i++) {
      var entry = root.media[i]
      if (entry.path === root.altPath) {
        entry = { path: entry.path, name: entry.name, blob: entry.blob,
          aspectRatio: entry.aspectRatio, failed: entry.failed,
          description: root.altDraft.trim() }
      }
      next.push(entry)
    }
    root.media = next
    root.closeAltText()
    keyCatcher.forceActiveFocus()
  }

  // ------------------------------------------------------------ posting

  function postStatus() {
    // A pending upload has no blob yet; posting now would leave it out.
    if (root.posting || root.busyWithMedia || !root.canPost) return
    root.posting = true
    root.postError = ""
    postProc.command = Model.postCmd(root.helperScript)
    postProc.environment = ({
      BLUESKY_POST_JSON: Model.postSpec(root.composerText, root.replyRef, root.media)
    })
    postProc.running = true
  }

  function onPostExited(exitCode) {
    root.posting = false
    postProc.environment = ({})
    if (root.checkSession(exitCode)) return
    var parsed = Model.parseJson(postOut.text)
    if (exitCode === 0 && parsed && parsed.uri) {
      root.setComposerText("")
      root.replyRef = null
      root.replyToUser = ""
      root.media = []
      root.closeAltText()
      root.loadFeeds()
    } else {
      // The images stay attached, so pressing Post again reuses the uploads.
      root.postError = Model.postErrorMessage(Model.errorName(postErr.text))
    }
  }

  // ------------------------------------------------------------ logout

  function clearState() {
    root.followingFeed = []
    root.discoverFeed = []
    root.mentionsFeed = []
    root.followingCursor = ""
    root.discoverCursor = ""
    root.mentionsCursor = ""
    root.followingHasMore = true
    root.discoverHasMore = true
    root.mentionsHasMore = true
    root.loadingFeed = false
    root.loadingMore = false
    root.feedError = ""
    root.setComposerText("")
    root.replyRef = null
    root.replyToUser = ""
    root.media = []
    root.mediaQueue = []
    root.uploadingPath = ""
    root.closeAltText()
    root.actionQueue = []
    root.pending = ({})
  }

  function logout() {
    if (root.hostWidget) root.hostWidget.clearAuth()
    root.clearState()
  }

  KeyboardPanel {
    id: panel
    anchorItem: root.anchorItem
    owner: root.hostWidget || root
    bar: root.bar
    open: root.opened
    centerOnBar: true
    focusTarget: keyCatcher
    contentWidth: panel.fittedContentWidth(Style.space(520))
    contentHeight: panel.fittedContentHeight(Style.space(700), Style.space(700))

    PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      // Keys.priority is BeforeItem, so without this the catcher's j/k/h/l and
      // space handling would eat the user's typing inside an editor.
      blocked: composerInput.activeFocus || identifierField.activeFocus
        || passwordField.activeFocus || serviceField.activeFocus || altField.activeFocus
      onCloseRequested: root.close()
      onTabRequested: function(direction) { root.switchPanel(direction) }

      Item {
        id: fixedTop
        anchors.top: parent.top
        anchors.left: parent.left
        anchors.right: parent.right
        // Chained with anchors and summed explicitly; a Column's implicitHeight
        // collapsed to 0 here in the Mastodon panel this one is modelled on.
        height: headerItem.height + loginRect.height + tabsRect.height
          + composerRect.height + Style.space(8) * 3

        Item {
          id: headerItem
          anchors.top: parent.top
          anchors.left: parent.left
          anchors.right: parent.right
          height: headerRow.height

          Row {
            id: headerRow
            spacing: Style.space(8)

            PanelSectionHeader {
              anchors.verticalCenter: parent.verticalCenter
              foreground: root.contentForeground
              fontFamily: root.contentFontFamily
              text: "BLUESKY"
            }

            Text {
              anchors.verticalCenter: parent.verticalCenter
              visible: root.authed
              text: root.authed ? "@" + root.auth.handle : ""
              color: Qt.darker(root.contentForeground, 1.6)
              font.family: root.contentFontFamily
              font.pixelSize: Style.font.caption
            }

            PanelActionButton {
              visible: root.authed
              iconText: ""
              tooltipText: "Reload"
              foreground: root.contentForeground
              fontFamily: root.contentFontFamily
              onClicked: root.loadFeeds()
            }

            PanelActionButton {
              visible: root.authed
              iconText: ""
              tooltipText: "Logout"
              foreground: root.contentForeground
              fontFamily: root.contentFontFamily
              onClicked: root.logout()
            }
          }
        }

        Rectangle {
          id: loginRect
          visible: !root.authed
          anchors.top: headerItem.bottom
          anchors.topMargin: Style.space(8)
          anchors.left: parent.left
          anchors.right: parent.right
          height: visible ? loginColumn.implicitHeight + Style.space(16) : 0
          radius: Style.cornerRadius
          color: Style.controlFill(false, false, root.contentForeground, Color.accent)

          Column {
            id: loginColumn
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.leftMargin: Style.space(12)
            anchors.rightMargin: Style.space(12)
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.space(8)

            Text {
              width: parent.width
              text: "Login with Bluesky"
              color: root.contentForeground
              font.family: root.contentFontFamily
              font.pixelSize: Style.font.body
              font.bold: true
            }

            TextField {
              id: identifierField
              width: parent.width
              foreground: root.contentForeground
              text: root.loginIdentifier
              placeholderText: "Handle (e.g. alice.bsky.social)"
              onTextEdited: root.loginIdentifier = text
              Keys.onReturnPressed: passwordField.forceActiveFocus()
              Keys.onEnterPressed: passwordField.forceActiveFocus()
              Keys.onEscapePressed: function(event) {
                keyCatcher.forceActiveFocus()
                event.accepted = true
              }
            }

            TextField {
              id: passwordField
              width: parent.width
              foreground: root.contentForeground
              password: true
              placeholderText: "App password"
              onAccepted: root.login()
              Keys.onEscapePressed: function(event) {
                keyCatcher.forceActiveFocus()
                event.accepted = true
              }
            }

            TextField {
              id: serviceField
              width: parent.width
              foreground: root.contentForeground
              text: root.loginService
              placeholderText: "Server (leave empty for bsky.social)"
              onTextEdited: root.loginService = text
              onAccepted: root.login()
              Keys.onEscapePressed: function(event) {
                keyCatcher.forceActiveFocus()
                event.accepted = true
              }
            }

            Text {
              width: parent.width
              text: "Use an app password, not your account password: Bluesky → Settings → Privacy and security → App passwords."
              color: Qt.darker(root.contentForeground, 1.6)
              font.family: root.contentFontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.WordWrap
            }

            Text {
              width: parent.width
              visible: root.loginError !== ""
              height: visible ? implicitHeight : 0
              text: root.loginError
              color: Color.urgent
              font.family: root.contentFontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.WordWrap
            }

            Button {
              text: root.loggingIn ? "Logging in..." : "Login"
              bordered: true
              focusable: true
              enabled: !root.loggingIn && root.loginIdentifier.trim() !== ""
              foreground: root.contentForeground
              fontFamily: root.contentFontFamily
              onClicked: root.login()
            }
          }
        }

        Rectangle {
          id: tabsRect
          visible: root.authed
          anchors.top: loginRect.bottom
          anchors.topMargin: Style.space(8)
          anchors.left: parent.left
          anchors.right: parent.right
          height: visible ? tabRow.implicitHeight + Style.space(12) : 0
          radius: Style.cornerRadius
          color: Style.controlFill(false, false, root.contentForeground, Color.accent)

          Row {
            id: tabRow
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.leftMargin: Style.space(8)
            anchors.rightMargin: Style.space(8)
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.space(4)

            Repeater {
              model: root.tabNames

              Button {
                required property string modelData
                required property int index
                text: modelData
                bordered: root.currentTab === index
                focusable: true
                foreground: root.contentForeground
                fontFamily: root.contentFontFamily
                onClicked: {
                  root.currentTab = index
                  scroll.contentY = 0
                }
              }
            }
          }
        }

        Rectangle {
          id: composerRect
          visible: root.authed
          anchors.top: tabsRect.bottom
          anchors.topMargin: Style.space(8)
          anchors.left: parent.left
          anchors.right: parent.right
          height: visible ? composerColumn.implicitHeight + Style.space(12) : 0
          radius: Style.cornerRadius
          color: Style.controlFill(false, false, root.contentForeground, Color.accent)

          Column {
            id: composerColumn
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.leftMargin: Style.space(12)
            anchors.rightMargin: Style.space(12)
            anchors.verticalCenter: parent.verticalCenter
            spacing: Style.space(6)

            Text {
              width: parent.width
              visible: root.replyToUser !== ""
              height: visible ? implicitHeight : 0
              text: "Replying to " + root.replyToUser
              color: Qt.darker(root.contentForeground, 1.5)
              font.family: root.contentFontFamily
              font.pixelSize: Style.font.caption
              elide: Text.ElideRight
            }

            Rectangle {
              width: parent.width
              height: Style.space(70)
              radius: Style.cornerRadius
              color: Style.controlFill(false, false, root.contentForeground, Color.accent)

              Text {
                anchors.left: parent.left
                anchors.leftMargin: Style.space(10)
                anchors.right: parent.right
                anchors.rightMargin: Style.space(10)
                anchors.top: parent.top
                anchors.topMargin: Style.space(8)
                visible: root.composerText === ""
                text: root.replyToUser !== "" ? "Write your reply" : "What's up?"
                color: Qt.darker(root.contentForeground, 1.6)
                font.family: root.contentFontFamily
                font.pixelSize: Style.font.body
                wrapMode: Text.WordWrap
              }

              // A TextEdit alone cannot scroll in Qt 6 (it has contentHeight
              // but no contentY). TextArea reports its wrapped height as
              // implicitHeight, and the ScrollView turns that into a range.
              ScrollView {
                id: composerScroll
                anchors.fill: parent
                anchors.margins: Style.space(8)
                clip: true
                background: Item {}
                ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
                ScrollBar.vertical.policy: composerInput.implicitHeight > composerScroll.height
                  ? ScrollBar.AsNeeded : ScrollBar.AlwaysOff
                Binding {
                  target: composerScroll.contentItem
                  property: "interactive"
                  // An interactive flickable would swallow the drag that
                  // selects text as soon as there is something to scroll.
                  value: composerInput.implicitHeight > composerScroll.height
                }

                TextArea {
                  id: composerInput
                  width: composerScroll.availableWidth
                  height: Math.max(implicitHeight, composerScroll.availableHeight)
                  background: Item {}
                  padding: 0
                  color: root.contentForeground
                  font.family: root.contentFontFamily
                  font.pixelSize: Style.font.body
                  wrapMode: TextEdit.Wrap
                  selectByMouse: true
                  onTextEdited: {
                    // 300 graphemes and 3000 bytes is the record limit. A
                    // longer post is refused by the server, so typing,
                    // pasting and drag and drop all stop at the limit.
                    var limited = Model.limitText(text, Model.MAX_POST_GRAPHEMES, Model.MAX_POST_BYTES)
                    if (limited !== text) {
                      var cursor = cursorPosition
                      text = limited
                      cursorPosition = Math.min(cursor, limited.length)
                    }
                    root.setComposerText(limited)
                  }
                  Keys.onEscapePressed: function(event) {
                    if (root.replyRef !== null) root.cancelReply()
                    keyCatcher.forceActiveFocus()
                    event.accepted = true
                  }
                }
              }
            }

            // The attached images, shown from the local file. A failed upload
            // keeps its slot with a warning, so it is obvious which file has
            // to be picked again; it has no blob and is left out of the post.
            Flow {
              width: parent.width
              visible: root.media.length > 0
              height: visible ? implicitHeight : 0
              spacing: Style.space(6)

              Repeater {
                model: root.media

                Rectangle {
                  id: mediaThumb
                  required property var modelData
                  readonly property bool uploading: !modelData.blob && !modelData.failed
                  width: Style.space(56)
                  height: Style.space(56)
                  radius: Style.cornerRadius
                  color: Style.controlFill(false, false, root.contentForeground, Color.accent)
                  clip: true

                  Image {
                    anchors.fill: parent
                    source: Model.fileUrl(mediaThumb.modelData.path)
                    // A phone photo is twelve megapixels; the thumbnail needs
                    // a hundred pixels of it.
                    sourceSize.width: Style.space(112)
                    sourceSize.height: Style.space(112)
                    fillMode: Image.PreserveAspectCrop
                    asynchronous: true
                    smooth: true
                    opacity: mediaThumb.uploading || mediaThumb.modelData.failed ? 0.4 : 1.0
                  }

                  Text {
                    anchors.centerIn: parent
                    visible: mediaThumb.uploading || mediaThumb.modelData.failed
                    text: mediaThumb.modelData.failed ? "!" : "…"
                    color: mediaThumb.modelData.failed ? Color.urgent : root.contentForeground
                    font.family: root.contentFontFamily
                    font.pixelSize: Style.font.body
                    font.bold: true
                  }

                  // Sized down so it does not cover a third of the thumbnail.
                  PanelActionButton {
                    anchors.top: parent.top
                    anchors.right: parent.right
                    size: Style.space(18)
                    fontSize: Style.font.body
                    iconText: ""
                    tooltipText: "Remove image"
                    hoverColor: Color.urgent
                    foreground: root.contentForeground
                    fontFamily: root.contentFontFamily
                    onClicked: root.removeMedia(mediaThumb.modelData)
                  }

                  // The badge says whether the image has an alt text; clicking
                  // the thumbnail opens the editor for it.
                  Rectangle {
                    anchors.bottom: parent.bottom
                    anchors.left: parent.left
                    anchors.margins: 1
                    width: altBadge.implicitWidth + Style.space(4)
                    height: altBadge.implicitHeight + Style.space(2)
                    radius: Style.cornerRadius
                    visible: !mediaThumb.modelData.failed
                    color: Qt.rgba(0, 0, 0, 0.55)

                    Text {
                      id: altBadge
                      anchors.centerIn: parent
                      text: (mediaThumb.modelData.description || "") !== "" ? "ALT" : "+ALT"
                      color: (mediaThumb.modelData.description || "") !== ""
                        ? Color.accent : root.contentForeground
                      font.family: root.contentFontFamily
                      font.pixelSize: Style.font.caption - 2
                    }
                  }

                  MouseArea {
                    anchors.fill: parent
                    anchors.rightMargin: Style.space(18)
                    enabled: !mediaThumb.modelData.failed
                    cursorShape: Qt.PointingHandCursor
                    onClicked: root.openAltText(mediaThumb.modelData)
                  }

                  Rectangle {
                    anchors.fill: parent
                    radius: Style.cornerRadius
                    color: "transparent"
                    border.width: Style.space(2)
                    border.color: Color.accent
                    visible: root.altPath === mediaThumb.modelData.path
                  }
                }
              }
            }

            Rectangle {
              id: altEditor
              width: parent.width
              visible: root.altPath !== ""
              height: visible ? implicitHeight : 0
              implicitHeight: altEditorColumn.implicitHeight + Style.space(8)
              radius: Style.cornerRadius
              color: Style.controlFill(false, false, root.contentForeground, Color.accent)

              Column {
                id: altEditorColumn
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.top: parent.top
                anchors.margins: Style.space(4)
                spacing: Style.space(4)

                Row {
                  width: parent.width
                  spacing: Style.space(4)

                  Text {
                    width: parent.width - altCounter.width - Style.space(4)
                    text: "Alt text"
                    elide: Text.ElideRight
                    color: Qt.darker(root.contentForeground, 1.5)
                    font.family: root.contentFontFamily
                    font.pixelSize: Style.font.caption
                  }

                  Text {
                    id: altCounter
                    text: root.altDraft.length + "/" + Model.MAX_ALT_TEXT
                    color: root.altDraft.length >= Model.MAX_ALT_TEXT
                      ? Color.urgent : Qt.darker(root.contentForeground, 1.7)
                    font.family: root.contentFontFamily
                    font.pixelSize: Style.font.caption
                  }
                }

                Row {
                  width: parent.width
                  spacing: Style.space(4)

                  TextField {
                    id: altField
                    width: parent.width - altSave.width - altDiscard.width - Style.space(8)
                    height: implicitHeight
                    text: ""
                    color: root.contentForeground
                    font.family: root.contentFontFamily
                    font.pixelSize: Style.font.body
                    placeholderText: "Describe this image"
                    background: Item {}
                    padding: 0
                    onTextChanged: {
                      var limited = Model.limitText(text, Model.MAX_ALT_TEXT, 0)
                      if (limited !== text) {
                        var cursor = cursorPosition
                        text = limited
                        cursorPosition = Math.min(cursor, limited.length)
                      }
                      root.altDraft = limited
                    }
                    Keys.onEscapePressed: function(event) {
                      root.closeAltText()
                      keyCatcher.forceActiveFocus()
                      event.accepted = true
                    }
                    Keys.onReturnPressed: function(event) {
                      event.accepted = true
                      root.saveAltText()
                    }
                    Keys.onEnterPressed: function(event) {
                      event.accepted = true
                      root.saveAltText()
                    }
                  }

                  Button {
                    id: altSave
                    text: "Save"
                    bordered: true
                    focusable: true
                    foreground: root.contentForeground
                    fontFamily: root.contentFontFamily
                    onClicked: root.saveAltText()
                  }

                  Button {
                    id: altDiscard
                    text: "Cancel"
                    bordered: true
                    focusable: true
                    foreground: root.contentForeground
                    fontFamily: root.contentFontFamily
                    onClicked: {
                      root.closeAltText()
                      keyCatcher.forceActiveFocus()
                    }
                  }
                }
              }
            }

            // Buttons from the left, the counter against the right edge.
            Item {
              width: parent.width
              height: Math.max(buttonRow.implicitHeight, composerCounter.height)

              Row {
                id: buttonRow
                anchors.left: parent.left
                anchors.right: composerCounter.left
                anchors.rightMargin: Style.space(8)
                anchors.verticalCenter: parent.verticalCenter
                spacing: Style.space(8)

                Button {
                  text: root.posting ? "Posting..." : "Post"
                  bordered: true
                  focusable: true
                  enabled: !root.posting && !root.busyWithMedia && root.canPost
                  foreground: root.contentForeground
                  fontFamily: root.contentFontFamily
                  onClicked: root.postStatus()
                }

                Button {
                  text: root.busyWithMedia ? "Adding..." : "Image"
                  bordered: true
                  focusable: true
                  enabled: !root.busyWithMedia && root.media.length < Model.MAX_IMAGES
                  foreground: root.contentForeground
                  fontFamily: root.contentFontFamily
                  onClicked: root.attachImages()
                }

                Button {
                  visible: root.replyRef !== null
                  text: "Cancel"
                  foreground: root.contentForeground
                  fontFamily: root.contentFontFamily
                  onClicked: root.cancelReply()
                }
              }

              Text {
                id: composerCounter
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                text: root.composerLength + "/" + Model.MAX_POST_GRAPHEMES
                color: root.composerLength >= Model.MAX_POST_GRAPHEMES
                  ? Color.urgent : Qt.darker(root.contentForeground, 1.7)
                font.family: root.contentFontFamily
                font.pixelSize: Style.font.caption
              }
            }

            Text {
              width: parent.width
              visible: root.postError !== ""
              height: visible ? implicitHeight : 0
              text: root.postError
              color: Color.urgent
              font.family: root.contentFontFamily
              font.pixelSize: Style.font.caption
              wrapMode: Text.WordWrap
            }
          }
        }
      }

      Flickable {
        id: scroll
        anchors.top: fixedTop.bottom
        anchors.topMargin: Style.space(8)
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.bottom: parent.bottom
        clip: true
        contentWidth: width
        contentHeight: contentColumn.implicitHeight
        boundsBehavior: Flickable.StopAtBounds
        onContentYChanged: root.maybeLoadMore()
        interactive: contentHeight > height

        Column {
          id: contentColumn
          width: scroll.width
          spacing: Style.space(8)

          Text {
            visible: root.authed && root.loadingFeed
            width: parent.width
            text: "Loading..."
            color: Qt.darker(root.contentForeground, 1.5)
            font.family: root.contentFontFamily
            font.pixelSize: Style.font.caption
          }

          Text {
            visible: root.authed && !root.loadingFeed && root.feedError !== ""
            width: parent.width
            text: root.feedError
            color: Color.urgent
            font.family: root.contentFontFamily
            font.pixelSize: Style.font.caption
            wrapMode: Text.WordWrap
          }

          Repeater {
            model: root.authed ? root.currentFeed : []

            Item {
              id: card
              required property var modelData
              readonly property var post: modelData.post
              readonly property var record: post.record || ({})
              readonly property var author: post.author || ({})
              readonly property var viewer: post.viewer || ({})
              readonly property var reposter: Model.reposter(modelData)
              readonly property var replyParent: Model.replyParentAuthor(modelData)
              readonly property string notification: Model.notificationLabel(modelData)
              // One call each, so the grid, its height and the alt text line
              // all read the same list and can never disagree.
              readonly property var media: Model.postMedia(post)
              readonly property int mediaCount: media.length
              readonly property var external: Model.postExternal(post)
              readonly property var quote: Model.postQuote(post)
              readonly property bool sensitive: Model.isSensitive(post)
              property bool mediaRevealed: false
              readonly property bool mediaVisible: mediaCount > 0 && (!sensitive || mediaRevealed)
              readonly property bool externalVisible: external !== null && (!sensitive || mediaRevealed)
              readonly property bool isOwn: author.did === root.auth.did
              width: parent.width
              height: cardRect.height
              implicitHeight: cardRect.height

              Rectangle {
                id: cardRect
                width: parent.width
                height: cardColumn.implicitHeight + Style.space(12)
                radius: Style.cornerRadius
                color: Style.controlFill(false, false, root.contentForeground, Color.accent)

                Column {
                  id: cardColumn
                  anchors.left: parent.left
                  anchors.right: parent.right
                  anchors.top: parent.top
                  anchors.leftMargin: Style.space(10)
                  anchors.rightMargin: Style.space(10)
                  anchors.topMargin: Style.space(6)
                  spacing: Style.space(4)

                  // visible: false does not stop QML from evaluating text, so
                  // every one of these guards its own null case.
                  Text {
                    width: parent.width
                    visible: card.reposter !== null
                    height: visible ? implicitHeight : 0
                    text: card.reposter !== null
                      ? "  " + Model.displayName(card.reposter) + " reposted" : ""
                    color: Qt.darker(root.contentForeground, 1.5)
                    font.family: root.contentFontFamily
                    font.pixelSize: Style.font.caption
                    elide: Text.ElideRight
                  }

                  Text {
                    width: parent.width
                    visible: card.notification !== ""
                    height: visible ? implicitHeight : 0
                    text: card.notification !== ""
                      ? "  " + Model.displayName(card.author) + " " + card.notification : ""
                    color: Qt.darker(root.contentForeground, 1.5)
                    font.family: root.contentFontFamily
                    font.pixelSize: Style.font.caption
                    elide: Text.ElideRight
                  }

                  Item {
                    width: parent.width
                    height: Math.max(nameRow.implicitHeight, followButton.visible ? followButton.implicitHeight : 0)

                    Row {
                      id: nameRow
                      anchors.left: parent.left
                      anchors.right: followButton.visible ? followButton.left : parent.right
                      anchors.rightMargin: Style.space(6)
                      anchors.verticalCenter: parent.verticalCenter
                      spacing: Style.space(6)

                      Text {
                        id: nameText
                        width: Math.min(implicitWidth, nameRow.width * 0.5)
                        text: Model.displayName(card.author)
                        color: root.contentForeground
                        font.family: root.contentFontFamily
                        font.pixelSize: Style.font.bodySmall
                        font.bold: true
                        elide: Text.ElideRight
                      }

                      Text {
                        anchors.verticalCenter: parent.verticalCenter
                        width: Math.min(implicitWidth, nameRow.width - nameText.width - timeText.width - Style.space(12))
                        text: Model.handle(card.author)
                        color: Qt.darker(root.contentForeground, 1.5)
                        font.family: root.contentFontFamily
                        font.pixelSize: Style.font.caption
                        elide: Text.ElideRight
                      }

                      Text {
                        id: timeText
                        anchors.verticalCenter: parent.verticalCenter
                        text: Model.formatTime(card.record.createdAt || card.post.indexedAt)
                        color: Qt.darker(root.contentForeground, 1.7)
                        font.family: root.contentFontFamily
                        font.pixelSize: Style.font.caption
                      }
                    }

                    Button {
                      id: followButton
                      anchors.right: parent.right
                      anchors.verticalCenter: parent.verticalCenter
                      visible: !card.isOwn && !Model.isFollowing(card.author)
                      text: root.isPending("follow", card.author.did) ? "..." : "Follow"
                      bordered: true
                      focusable: true
                      foreground: root.contentForeground
                      fontFamily: root.contentFontFamily
                      onClicked: root.follow(card.author)
                    }
                  }

                  Text {
                    width: parent.width
                    visible: card.replyParent !== null
                    height: visible ? implicitHeight : 0
                    text: card.replyParent !== null
                      ? "  Replying to " + Model.handle(card.replyParent) : ""
                    color: Qt.darker(root.contentForeground, 1.7)
                    font.family: root.contentFontFamily
                    font.pixelSize: Style.font.caption
                    elide: Text.ElideRight
                  }

                  // RichText ignores the item's color, so both colours are
                  // inlined by postRichText. Only faceted ranges become links,
                  // and every other character of the post is escaped.
                  Text {
                    width: parent.width
                    visible: Model.plainText(card.record) !== ""
                    height: visible ? implicitHeight : 0
                    textFormat: Text.RichText
                    text: Model.postRichText(card.record, String(root.contentForeground), String(Color.accent))
                    color: root.contentForeground
                    font.family: root.contentFontFamily
                    font.pixelSize: Style.font.body
                    wrapMode: Text.Wrap
                    onLinkActivated: function(url) { root.openExternal(url) }
                  }

                  Button {
                    visible: card.sensitive && !card.mediaRevealed
                      && (card.mediaCount > 0 || card.external !== null)
                    text: "Show media"
                    bordered: true
                    focusable: true
                    foreground: root.contentForeground
                    fontFamily: root.contentFontFamily
                    onClicked: card.mediaRevealed = true
                  }

                  Grid {
                    id: mediaGrid
                    width: parent.width
                    visible: card.mediaVisible
                    columns: card.mediaCount > 1 ? 2 : 1
                    spacing: Style.space(4)

                    readonly property real cellWidth: (width - spacing * (columns - 1)) / columns
                    readonly property real cellHeight: columns > 1 ? Style.space(96) : Style.space(170)
                    readonly property int rows: Math.ceil(card.mediaCount / columns)
                    // Explicit rather than implicit, so the card always knows
                    // how tall its media is even while it is still loading.
                    height: visible && rows > 0 ? rows * cellHeight + (rows - 1) * spacing : 0

                    Repeater {
                      model: card.mediaVisible ? card.media : []

                      Rectangle {
                        required property var modelData
                        width: mediaGrid.cellWidth
                        height: mediaGrid.cellHeight
                        radius: Style.cornerRadius
                        color: Style.controlFill(false, false, root.contentForeground, Color.accent)
                        clip: true

                        Image {
                          anchors.fill: parent
                          source: modelData.url
                          sourceSize.width: Style.space(520)
                          fillMode: Image.PreserveAspectCrop
                          asynchronous: true
                          smooth: true
                          cache: true
                        }

                        // A video is not played here; its thumbnail opens
                        // the post on bsky.app.
                        Rectangle {
                          anchors.centerIn: parent
                          visible: modelData.video
                          width: Style.space(40)
                          height: Style.space(40)
                          radius: width / 2
                          color: Qt.rgba(0, 0, 0, 0.6)

                          Text {
                            anchors.centerIn: parent
                            anchors.horizontalCenterOffset: Style.space(2)
                            text: ""
                            color: "white"
                            font.family: root.contentFontFamily
                            font.pixelSize: Style.font.body
                          }
                        }

                        MouseArea {
                          anchors.fill: parent
                          cursorShape: Qt.PointingHandCursor
                          onClicked: root.openExternal(modelData.video ? modelData.link : modelData.fullUrl)
                        }
                      }
                    }
                  }

                  Text {
                    width: parent.width
                    readonly property string altText: {
                      var parts = []
                      for (var i = 0; i < card.media.length; i++) {
                        if (card.media[i].description !== "") parts.push(card.media[i].description)
                      }
                      return parts.join("  ·  ")
                    }
                    text: altText
                    visible: card.mediaVisible && altText !== ""
                    height: visible ? implicitHeight : 0
                    color: Qt.darker(root.contentForeground, 1.7)
                    font.family: root.contentFontFamily
                    font.pixelSize: Style.font.caption
                    wrapMode: Text.Wrap
                    maximumLineCount: 3
                    elide: Text.ElideRight
                  }

                  // Link card: the page's thumbnail, title and domain.
                  Rectangle {
                    id: externalCard
                    width: parent.width
                    visible: card.externalVisible
                    height: visible ? Math.max(externalText.implicitHeight, externalThumb.visible ? externalThumb.height : 0) + Style.space(8) : 0
                    radius: Style.cornerRadius
                    color: "transparent"
                    border.width: 1
                    border.color: Qt.darker(root.contentForeground, 2.5)
                    clip: true

                    Image {
                      id: externalThumb
                      anchors.left: parent.left
                      anchors.top: parent.top
                      anchors.margins: Style.space(4)
                      visible: card.externalVisible && card.external.image !== ""
                      width: visible ? Style.space(64) : 0
                      height: Style.space(64)
                      source: visible ? card.external.image : ""
                      sourceSize.width: Style.space(128)
                      fillMode: Image.PreserveAspectCrop
                      asynchronous: true
                      cache: true
                    }

                    Column {
                      id: externalText
                      anchors.left: externalThumb.right
                      anchors.leftMargin: Style.space(8)
                      anchors.right: parent.right
                      anchors.rightMargin: Style.space(8)
                      anchors.top: parent.top
                      anchors.topMargin: Style.space(4)
                      spacing: Style.space(2)

                      Text {
                        width: parent.width
                        text: card.external ? card.external.title : ""
                        color: root.contentForeground
                        font.family: root.contentFontFamily
                        font.pixelSize: Style.font.bodySmall
                        font.bold: true
                        wrapMode: Text.Wrap
                        maximumLineCount: 2
                        elide: Text.ElideRight
                      }

                      Text {
                        width: parent.width
                        visible: card.external !== null && card.external.description !== ""
                        height: visible ? implicitHeight : 0
                        text: card.external ? card.external.description : ""
                        color: Qt.darker(root.contentForeground, 1.4)
                        font.family: root.contentFontFamily
                        font.pixelSize: Style.font.caption
                        wrapMode: Text.Wrap
                        maximumLineCount: 2
                        elide: Text.ElideRight
                      }

                      Text {
                        width: parent.width
                        text: card.external ? card.external.domain : ""
                        color: Qt.darker(root.contentForeground, 1.7)
                        font.family: root.contentFontFamily
                        font.pixelSize: Style.font.caption
                        elide: Text.ElideRight
                      }
                    }

                    MouseArea {
                      anchors.fill: parent
                      cursorShape: Qt.PointingHandCursor
                      onClicked: if (card.external) root.openExternal(card.external.url)
                    }
                  }

                  // A quoted post, or a one-line note for what cannot be shown.
                  Rectangle {
                    id: quoteCard
                    width: parent.width
                    visible: card.quote !== null
                    height: visible ? quoteColumn.implicitHeight + Style.space(10) : 0
                    radius: Style.cornerRadius
                    color: "transparent"
                    border.width: 1
                    border.color: Qt.darker(root.contentForeground, 2.5)

                    Column {
                      id: quoteColumn
                      anchors.left: parent.left
                      anchors.right: parent.right
                      anchors.top: parent.top
                      anchors.margins: Style.space(6)
                      spacing: Style.space(2)

                      Text {
                        width: parent.width
                        visible: card.quote !== null && card.quote.author !== null
                        height: visible ? implicitHeight : 0
                        text: card.quote && card.quote.author
                          ? Model.displayName(card.quote.author) + "  " + Model.handle(card.quote.author)
                            + "  ·  " + Model.formatTime(card.quote.createdAt)
                          : ""
                        color: Qt.darker(root.contentForeground, 1.3)
                        font.family: root.contentFontFamily
                        font.pixelSize: Style.font.caption
                        font.bold: true
                        elide: Text.ElideRight
                      }

                      Text {
                        width: parent.width
                        text: card.quote ? (card.quote.author ? card.quote.text : card.quote.note) : ""
                        textFormat: Text.PlainText
                        color: card.quote && card.quote.author
                          ? root.contentForeground : Qt.darker(root.contentForeground, 1.7)
                        font.family: root.contentFontFamily
                        font.pixelSize: Style.font.bodySmall
                        font.italic: card.quote !== null && card.quote.author === null
                        wrapMode: Text.Wrap
                        maximumLineCount: 5
                        elide: Text.ElideRight
                      }
                    }

                    MouseArea {
                      anchors.fill: parent
                      enabled: card.quote !== null && card.quote.url !== ""
                      cursorShape: enabled ? Qt.PointingHandCursor : Qt.ArrowCursor
                      onClicked: root.openExternal(card.quote.url)
                    }
                  }

                  Row {
                    spacing: Style.space(2)
                    height: replyButton.height

                    PanelActionButton {
                      id: replyButton
                      iconText: ""
                      tooltipText: card.viewer.replyDisabled ? "Replies are turned off" : "Reply"
                      enabled: card.viewer.replyDisabled !== true
                      foreground: root.contentForeground
                      fontFamily: root.contentFontFamily
                      onClicked: root.startReply(card.post)
                    }

                    Text {
                      anchors.verticalCenter: parent.verticalCenter
                      width: Style.space(34)
                      text: Model.count(card.post.replyCount)
                      color: Qt.darker(root.contentForeground, 1.6)
                      font.family: root.contentFontFamily
                      font.pixelSize: Style.font.caption
                    }

                    PanelActionButton {
                      iconText: ""
                      tooltipText: card.viewer.repost ? "Undo repost" : "Repost"
                      enabled: !root.isPending("repost", card.post.uri)
                      foreground: card.viewer.repost ? Color.accent : root.contentForeground
                      fontFamily: root.contentFontFamily
                      onClicked: root.toggleRepost(card.post)
                    }

                    Text {
                      anchors.verticalCenter: parent.verticalCenter
                      width: Style.space(34)
                      text: Model.count(card.post.repostCount)
                      color: Qt.darker(root.contentForeground, 1.6)
                      font.family: root.contentFontFamily
                      font.pixelSize: Style.font.caption
                    }

                    PanelActionButton {
                      iconText: ""
                      tooltipText: card.viewer.like ? "Unlike" : "Like"
                      enabled: !root.isPending("like", card.post.uri)
                      foreground: card.viewer.like ? Color.accent : root.contentForeground
                      fontFamily: root.contentFontFamily
                      onClicked: root.toggleLike(card.post)
                    }

                    Text {
                      anchors.verticalCenter: parent.verticalCenter
                      width: Style.space(34)
                      text: Model.count(card.post.likeCount)
                      color: Qt.darker(root.contentForeground, 1.6)
                      font.family: root.contentFontFamily
                      font.pixelSize: Style.font.caption
                    }

                    PanelActionButton {
                      iconText: ""
                      tooltipText: card.viewer.bookmarked ? "Remove bookmark" : "Bookmark"
                      enabled: !root.isPending("bookmark", card.post.uri)
                      foreground: card.viewer.bookmarked ? Color.accent : root.contentForeground
                      fontFamily: root.contentFontFamily
                      onClicked: root.toggleBookmark(card.post)
                    }

                    Item { width: Style.space(8); height: 1 }

                    PanelActionButton {
                      iconText: ""
                      tooltipText: "Open on bsky.app"
                      foreground: root.contentForeground
                      fontFamily: root.contentFontFamily
                      onClicked: root.openExternal(Model.postWebUrl(card.post))
                    }
                  }
                }
              }
            }
          }

          Text {
            width: parent.width
            visible: root.authed && !root.loadingFeed && root.feedEmpty && root.feedError === ""
            text: root.currentTab === 0 ? "Nothing here yet. Follow some people to fill this feed."
              : (root.currentTab === 1 ? "The Discover feed is empty right now" : "No mentions")
            color: Qt.darker(root.contentForeground, 1.7)
            font.family: root.contentFontFamily
            font.pixelSize: Style.font.caption
            horizontalAlignment: Text.AlignHCenter
            wrapMode: Text.WordWrap
          }

          Text {
            width: parent.width
            visible: root.authed && !root.loadingFeed && !root.feedEmpty
            text: root.loadingMore ? "Loading older posts…"
              : (root.hasMore ? "Scroll for more" : "No older posts")
            color: Qt.darker(root.contentForeground, 1.7)
            font.family: root.contentFontFamily
            font.pixelSize: Style.font.caption
            horizontalAlignment: Text.AlignHCenter
          }
        }
      }
    }
  }

  Process {
    id: followingProc
    stdout: StdioCollector {
      id: followingOut
      waitForEnd: true
    }
    onExited: function(exitCode) {
      root.onFollowingExited(exitCode)
    }
  }

  Process {
    id: discoverProc
    stdout: StdioCollector {
      id: discoverOut
      waitForEnd: true
    }
    onExited: function(exitCode) {
      root.onDiscoverExited(exitCode)
    }
  }

  Process {
    id: mentionsProc
    stdout: StdioCollector {
      id: mentionsOut
      waitForEnd: true
    }
    onExited: function(exitCode) {
      root.onMentionsExited(exitCode)
    }
  }

  // One process for older pages of whichever tab is showing; `tab` remembers
  // which one asked, in case the user switches tabs while it runs.
  Process {
    id: moreProc
    property int tab: 0
    stdout: StdioCollector {
      id: moreOut
      waitForEnd: true
    }
    onExited: function(exitCode) {
      root.onMoreExited(exitCode)
    }
  }

  Process {
    id: actionProc
    stdout: StdioCollector {
      id: actionOut
      waitForEnd: true
    }
    onExited: function(exitCode) {
      root.onActionExited(exitCode)
    }
  }

  Process {
    id: pickMediaProc
    stdout: StdioCollector {
      id: pickMediaOut
      waitForEnd: true
    }
    onExited: function(exitCode) {
      root.onPickMediaExited(exitCode)
    }
  }

  Process {
    id: uploadMediaProc
    stdout: StdioCollector {
      id: uploadMediaOut
      waitForEnd: true
    }
    stderr: StdioCollector {
      id: uploadMediaErr
      waitForEnd: true
    }
    onExited: function(exitCode) {
      root.onUploadMediaExited(exitCode)
    }
  }

  Process {
    id: postProc
    stdout: StdioCollector {
      id: postOut
      waitForEnd: true
    }
    stderr: StdioCollector {
      id: postErr
      waitForEnd: true
    }
    onExited: function(exitCode) {
      root.onPostExited(exitCode)
    }
  }
}
