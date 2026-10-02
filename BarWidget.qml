import QtQuick
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui
import "Model.js" as Model

BarWidget {
  id: root
  moduleName: "saigkill.bluesky"

  // The session tokens never enter this file: bluesky_helper.py owns the 0600
  // state file and both tokens. This file only ever sees the server, the
  // handle, the DID, and whether a session exists. The app password passes
  // through once, on its way into the helper's environment.
  readonly property string helperScript: Qt.resolvedUrl("bluesky_helper.py").toString().replace("file://", "")

  property var auth: Model.emptyAuth()
  readonly property bool authed: Model.isAuthed(auth)
  property bool loginPending: false
  property string loginError: ""
  property string pendingService: ""
  property bool pendingLoginSuccess: false

  readonly property string tooltipText: authed
    ? "Bluesky: @" + auth.handle
    : "Bluesky: not logged in"

  readonly property color stateColor: authed
    ? (bar ? bar.barForeground : Color.foreground)
    : Color.urgent

  function loadData() {
    loadProc.command = Model.loadCmd(root.helperScript)
    loadProc.running = true
  }

  function onLoadExited(exitCode) {
    var data = Model.decode(Model.parseJson(loadOut.text))
    root.auth = data.auth
    if (root.pendingLoginSuccess) {
      root.pendingLoginSuccess = false
      if (panelLoader.item) panelLoader.item.onLoginSuccess()
    }
  }

  // The password goes into the helper's environment (BLUESKY_APP_PASSWORD) and
  // nowhere else: /proc/<pid>/environ is readable by the owner only, while
  // /proc/<pid>/cmdline is readable by everyone. It is cleared off the Process
  // again as soon as the helper has exited.
  function startLogin(service, identifier, password) {
    var normalized = Model.normalizeService(service)
    if (normalized === "") {
      root.loginError = Model.loginErrorMessage("insecure_service", service)
      return
    }
    if (Model.normalizeIdentifier(identifier) === "" || String(password || "") === "") {
      root.loginError = "Enter your handle and an app password"
      return
    }
    root.loginError = ""
    root.loginPending = true
    root.pendingService = normalized
    loginProc.command = Model.loginCmd(root.helperScript, normalized, identifier)
    loginProc.environment = ({ BLUESKY_APP_PASSWORD: String(password) })
    loginProc.running = true
  }

  function onLoginExited(exitCode) {
    loginProc.environment = ({})
    root.loginPending = false
    var parsed = Model.parseJson(loginOut.text)
    if (exitCode !== 0 || !parsed || !parsed.auth || parsed.auth.hasSession !== true) {
      root.loginError = Model.loginErrorMessage(Model.errorName(loginErr.text), root.pendingService)
      return
    }
    root.loginError = ""
    root.pendingLoginSuccess = true
    root.loadData()
  }

  function clearAuth() {
    root.loginError = ""
    root.loginPending = false
    logoutProc.command = Model.logoutCmd(root.helperScript)
    logoutProc.running = true
  }

  // The helper ends the session itself when the refresh token is refused, so
  // re-reading the state is enough to bring the login form back.
  function sessionExpired() {
    root.loginError = "Your session has expired. Please log in again."
    root.loadData()
  }

  readonly property bool opened: panelLoader.item ? panelLoader.item.opened === true : false
  readonly property bool popoutSwitchClosing: panelLoader.item ? panelLoader.item.popoutSwitchClosing === true : false

  function open() { if (panelLoader.item) panelLoader.item.open() }
  function close() { if (panelLoader.item) panelLoader.item.close() }
  function toggle() { if (panelLoader.item) panelLoader.item.toggle() }
  function closeForPopoutSwitch() { if (panelLoader.item) panelLoader.item.closeForPopoutSwitch() }

  function injectPanel() {
    var target = panelLoader.item
    if (!target) return
    if ("bar" in target) target.bar = root.bar
    if ("settings" in target) target.settings = root.settings
    if ("anchorItem" in target) target.anchorItem = button
    if ("hostWidget" in target) target.hostWidget = root
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  onBarChanged: injectPanel()
  onSettingsChanged: injectPanel()
  Component.onCompleted: loadData()

  Loader {
    id: panelLoader
    active: true
    source: Qt.resolvedUrl("Panel.qml")
    visible: false
    onLoaded: {
      root.injectPanel()
      Qt.callLater(root.injectPanel)
    }
  }

  IpcHandler {
    target: "saigkill.bluesky"
    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    // JetBrainsMono Nerd Font has no Bluesky glyph. md-butterfly (U+F1589) is
    // the closest to the logo. It lies outside the BMP, so it is written as its
    // UTF-16 surrogate pair.
    text: "\uDB85\uDD89"
    slotSize: Style.bar.statusSlot
    fontSize: Style.font.caption
    active: false
    enabled: true
    foreground: root.stateColor
    tooltipText: root.tooltipText

    onPressed: function(mouseButton) {
      if (mouseButton === Qt.LeftButton) root.toggle()
    }
  }

  Process {
    id: loadProc
    stdout: StdioCollector {
      id: loadOut
      waitForEnd: true
    }
    onExited: function(exitCode) {
      root.onLoadExited(exitCode)
    }
  }

  Process {
    id: loginProc
    stdout: StdioCollector {
      id: loginOut
      waitForEnd: true
    }
    stderr: StdioCollector {
      id: loginErr
      waitForEnd: true
    }
    onExited: function(exitCode) {
      root.onLoginExited(exitCode)
    }
  }

  Process {
    id: logoutProc
    onExited: function(exitCode) {
      root.loadData()
    }
  }
}
