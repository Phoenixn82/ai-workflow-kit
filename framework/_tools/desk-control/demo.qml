import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Io

ShellRoot {
    FloatingWindow {
        id: window
        title: "the user Computer Use — Safe Test"
        width: 650; height: 460; color: "#101827"
        property int clicks: 0
        Column {
            anchors.centerIn: parent; spacing: 25; width: parent.width - 70
            Text { text: "LOCAL DESKTOP CONTROL TEST"; color: "#60a5fa"; font.pixelSize: 20; font.bold: true }
            Text { text: "Harmless test window · no printer commands"; color: "#cbd5e1"; font.pixelSize: 15 }
            TextField { id: entry; width: parent.width; placeholderText: "Type here"; font.pixelSize: 18; Accessible.name: "Test text field" }
            Button { text: "Click test button"; onClicked: { window.clicks++; result.text = "Clicked " + window.clicks + " time(s) — " + entry.text } }
            Text { id: result; text: "Waiting for test"; color: "#86efac"; font.pixelSize: 17; wrapMode: Text.Wrap; width: parent.width }
        }
    }
}
