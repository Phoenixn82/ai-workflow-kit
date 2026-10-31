import QtQuick
import Quickshell
import Quickshell.Wayland
import Quickshell.Io

ShellRoot {
    id: root
    readonly property string token: Quickshell.env("DESK_SESSION")
    readonly property string python: Quickshell.env("DESK_PYTHON")
    readonly property string script: Quickshell.env("DESK_SCRIPT")
    property bool stopping: false
    PanelWindow {
        id: overlay
        screen: Quickshell.screens.find(s => s.name === Quickshell.env("DESK_MONITOR")) ?? null
        anchors { top: true; bottom: true; left: true; right: true }
        color: "transparent"
        exclusionMode: ExclusionMode.Ignore
        WlrLayershell.namespace: "desk-control"
        WlrLayershell.layer: WlrLayer.Overlay
        WlrLayershell.keyboardFocus: WlrKeyboardFocus.None
        mask: Region { item: badge }
        Rectangle {
            anchors.fill: parent
            color: "transparent"
            border.color: root.stopping ? "#fb7185" : "#60a5fa"
            border.width: 4
        }
        Rectangle {
            id: badge
            anchors.top: parent.top
            anchors.horizontalCenter: parent.horizontalCenter
            width: Math.min(590, parent.width)
            height: 32
            color: root.stopping ? "#881337" : "#173977"
            radius: 6
            Text {
                anchors.left: parent.left; anchors.leftMargin: 12
                anchors.verticalCenter: parent.verticalCenter
                width: parent.width - 126
                color: "white"; font.pixelSize: 12; elide: Text.ElideRight
                text: root.stopping ? "Stopped — application stays open" : Quickshell.env("DESK_AGENT") + " controlling " + Quickshell.env("DESK_APP") + " · Super+Ctrl+Esc stops"
            }
            Rectangle {
                anchors.right: parent.right; anchors.rightMargin: 5
                anchors.verticalCenter: parent.verticalCenter
                width: 92; height: 24; radius: 4; color: "#be123c"
                Text { anchors.centerIn: parent; text: "STOP"; color: "white"; font.bold: true }
                MouseArea {
                    anchors.fill: parent
                    onClicked: { root.stopping = true; stop.running = true }
                }
            }
        }
    }
    Process {
        id: beat
        command: [root.python, root.script, "heartbeat", root.token]
        onExited: (exitCode, exitStatus) => { if (exitCode !== 0) Qt.quit() }
    }
    Process { id: stop; command: [root.python, root.script, "stop"] }
    Timer {
        interval: 450; running: overlay.visible && !root.stopping; repeat: true; triggeredOnStart: true
        onTriggered: if (!beat.running) beat.running = true
    }
}
