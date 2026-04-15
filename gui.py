#!/usr/bin/env python3
"""Zwoosh GUI — PySide6 desktop application for the Zwift Click BLE bridge."""

import sys
import time

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QTabWidget, QTextEdit, QLineEdit, QSpinBox,
    QCheckBox, QFrame, QSystemTrayIcon, QMenu, QSizePolicy,
)
from PySide6.QtCore import Qt, Signal, QObject, QTimer
from PySide6.QtGui import QIcon, QColor, QPainter, QPixmap, QFont, QTextCursor, QAction

from core import ZwooshCore, load_config, save_config, DEFAULT_CONFIG


# ---------------------------------------------------------------------------
# Dark theme stylesheet
# ---------------------------------------------------------------------------

DARK_STYLE = """
QMainWindow, QWidget {
    background-color: #1a1a2e;
    color: #e0e0e0;
    font-family: "Segoe UI";
    font-size: 13px;
}

QLabel {
    color: #e0e0e0;
}

QPushButton {
    padding: 8px 18px;
    border-radius: 6px;
    font-weight: 600;
    font-size: 13px;
    border: none;
}

QPushButton#connectBtn {
    background-color: #ff6b35;
    color: white;
}
QPushButton#connectBtn:hover {
    background-color: #ff8555;
}
QPushButton#connectBtn:disabled {
    background-color: #555;
    color: #888;
}

QPushButton#disconnectBtn {
    background-color: #c0392b;
    color: white;
}
QPushButton#disconnectBtn:hover {
    background-color: #e04838;
}

QPushButton#secondaryBtn {
    background-color: #1a4a7a;
    color: #ccc;
    border: 1px solid #2a5a8a;
}
QPushButton#secondaryBtn:hover {
    background-color: #2a5a8a;
}

QPushButton#primaryBtn {
    background-color: #ff6b35;
    color: white;
}
QPushButton#primaryBtn:hover {
    background-color: #ff8555;
}

QTabWidget::pane {
    border: none;
    background-color: #1a1a2e;
}
QTabBar::tab {
    padding: 8px 16px;
    color: #888;
    border-bottom: 2px solid transparent;
    background: transparent;
    font-size: 13px;
}
QTabBar::tab:selected {
    color: #ff6b35;
    border-bottom: 2px solid #ff6b35;
}
QTabBar::tab:hover {
    color: #ccc;
}

QTextEdit {
    background-color: #0a0a1a;
    color: #e0e0e0;
    border: none;
    border-radius: 8px;
    padding: 10px;
    font-family: "Cascadia Code", "Consolas", monospace;
    font-size: 12px;
}

QLineEdit, QSpinBox {
    background-color: #0a0a1a;
    border: 1px solid #333;
    border-radius: 4px;
    padding: 6px 10px;
    color: #e0e0e0;
    font-size: 13px;
}
QLineEdit:focus, QSpinBox:focus {
    border-color: #ff6b35;
}

QCheckBox {
    color: #ccc;
    font-size: 13px;
    spacing: 8px;
}
QCheckBox::indicator {
    width: 18px;
    height: 18px;
    border-radius: 4px;
    border: 1px solid #555;
    background: #0a0a1a;
}
QCheckBox::indicator:checked {
    background: #ff6b35;
    border-color: #ff6b35;
}

QFrame#statusBar {
    background-color: #0a0a1a;
    border-radius: 8px;
    padding: 10px;
}

QFrame#deviceCard {
    background-color: #0f3460;
    border: 1px solid #1a4a7a;
    border-radius: 8px;
    padding: 10px;
}
"""


# ---------------------------------------------------------------------------
# Signal bridge (thread-safe core -> GUI communication)
# ---------------------------------------------------------------------------

class CoreSignals(QObject):
    log = Signal(str, str, str, str)           # ts, level, source, message
    device_found = Signal(str, str, int)        # name, address, rssi
    connected = Signal(str, str, object)        # label, address, battery (int|None)
    disconnected = Signal(str)                  # label
    shift = Signal(str, str, int)               # label, direction, gear
    state_changed = Signal(str)                 # state string


# ---------------------------------------------------------------------------
# Tray icon helpers
# ---------------------------------------------------------------------------

def _make_colored_icon(color: str) -> QIcon:
    """Create a small solid-colored circle icon for the tray."""
    size = 64
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.transparent)
    painter = QPainter(pixmap)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setBrush(QColor(color))
    painter.setPen(Qt.NoPen)
    painter.drawEllipse(4, 4, size - 8, size - 8)
    painter.end()
    return QIcon(pixmap)


# ---------------------------------------------------------------------------
# Device card widget
# ---------------------------------------------------------------------------

class DeviceCard(QFrame):
    def __init__(self, label: str, address: str, battery: int | None):
        super().__init__()
        self.setObjectName("deviceCard")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(10, 10, 10, 10)
        layout.setSpacing(4)

        name_label = QLabel(label)
        name_label.setStyleSheet("font-weight: 600; font-size: 13px;")
        layout.addWidget(name_label)

        addr_label = QLabel(address)
        addr_label.setStyleSheet("font-size: 11px; color: #666; font-family: monospace;")
        layout.addWidget(addr_label)

        self.battery_label = QLabel()
        self._set_battery(battery)
        self.battery_label.setStyleSheet("font-size: 12px; color: #2ecc71;")
        layout.addWidget(self.battery_label)

        self.device_label = label

    def _set_battery(self, battery: int | None):
        if battery is not None:
            self.battery_label.setText(f"Battery: {battery}%")
        else:
            self.battery_label.setText("Battery: unknown")


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class ZwooshWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Zwoosh")
        self.setFixedSize(520, 580)

        self.cfg = load_config()
        self.core = ZwooshCore(self.cfg)
        self.signals = CoreSignals()
        self._device_cards: dict[str, DeviceCard] = {}
        self._state = "disconnected"

        self._setup_core_callbacks()
        self._build_ui()
        self._setup_tray()
        self._connect_signals()
        self._update_ui_state("disconnected")

    # ---- core callback -> signal bridge ----

    def _setup_core_callbacks(self):
        s = self.signals
        self.core.on_log = s.log.emit
        self.core.on_device_found = s.device_found.emit
        self.core.on_connected = s.connected.emit
        self.core.on_disconnected = s.disconnected.emit
        self.core.on_shift = s.shift.emit
        self.core.on_state_changed = s.state_changed.emit

    def _connect_signals(self):
        s = self.signals
        s.log.connect(self._on_log)
        s.connected.connect(self._on_connected)
        s.disconnected.connect(self._on_disconnected)
        s.state_changed.connect(self._update_ui_state)

    # ---- build UI ----

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        root = QVBoxLayout(central)
        root.setContentsMargins(16, 16, 16, 16)
        root.setSpacing(12)

        # Status bar
        self.status_frame = QFrame()
        self.status_frame.setObjectName("statusBar")
        status_layout = QHBoxLayout(self.status_frame)
        status_layout.setContentsMargins(12, 8, 12, 8)

        self.status_dot = QLabel()
        self.status_dot.setFixedSize(12, 12)
        status_layout.addWidget(self.status_dot)

        self.status_text = QLabel("Disconnected")
        self.status_text.setStyleSheet("font-size: 13px;")
        status_layout.addWidget(self.status_text)
        status_layout.addStretch()

        root.addWidget(self.status_frame)

        # Device cards container
        self.devices_layout = QHBoxLayout()
        self.devices_layout.setSpacing(10)
        self.devices_container = QWidget()
        self.devices_container.setLayout(self.devices_layout)
        self.devices_container.setVisible(False)
        root.addWidget(self.devices_container)

        # Buttons row
        btn_layout = QHBoxLayout()
        btn_layout.setSpacing(8)

        self.connect_btn = QPushButton("Connect")
        self.connect_btn.setObjectName("connectBtn")
        self.connect_btn.clicked.connect(self._on_connect_clicked)
        btn_layout.addWidget(self.connect_btn)

        self.disconnect_btn = QPushButton("Disconnect")
        self.disconnect_btn.setObjectName("disconnectBtn")
        self.disconnect_btn.clicked.connect(self._on_disconnect_clicked)
        btn_layout.addWidget(self.disconnect_btn)

        self.cancel_btn = QPushButton("Cancel")
        self.cancel_btn.setObjectName("secondaryBtn")
        self.cancel_btn.clicked.connect(self._on_disconnect_clicked)
        btn_layout.addWidget(self.cancel_btn)

        self.clear_btn = QPushButton("Clear Logs")
        self.clear_btn.setObjectName("secondaryBtn")
        self.clear_btn.clicked.connect(self._clear_logs)
        btn_layout.addWidget(self.clear_btn)

        btn_layout.addStretch()
        root.addLayout(btn_layout)

        # Tab widget
        self.tabs = QTabWidget()
        root.addWidget(self.tabs)

        # --- Logs tab ---
        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        self.tabs.addTab(self.log_text, "Logs")

        # --- Settings tab ---
        settings_widget = QWidget()
        settings_layout = QVBoxLayout(settings_widget)
        settings_layout.setContentsMargins(8, 12, 8, 8)
        settings_layout.setSpacing(16)

        # Key mappings section
        key_section_label = QLabel("KEY MAPPINGS")
        key_section_label.setStyleSheet("font-size: 11px; color: #ff6b35; font-weight: 600; letter-spacing: 0.5px;")
        settings_layout.addWidget(key_section_label)

        key_grid = QHBoxLayout()
        key_grid.setSpacing(12)

        up_col = QVBoxLayout()
        up_col.addWidget(QLabel("Shift Up Key"))
        self.shift_up_input = QLineEdit(self.cfg["shift_up_key"])
        self.shift_up_input.setMaximumWidth(120)
        up_col.addWidget(self.shift_up_input)
        key_grid.addLayout(up_col)

        down_col = QVBoxLayout()
        down_col.addWidget(QLabel("Shift Down Key"))
        self.shift_down_input = QLineEdit(self.cfg["shift_down_key"])
        self.shift_down_input.setMaximumWidth(120)
        down_col.addWidget(self.shift_down_input)
        key_grid.addLayout(down_col)

        key_grid.addStretch()
        settings_layout.addLayout(key_grid)

        # Connection section
        conn_section_label = QLabel("CONNECTION")
        conn_section_label.setStyleSheet("font-size: 11px; color: #ff6b35; font-weight: 600; letter-spacing: 0.5px;")
        settings_layout.addWidget(conn_section_label)

        conn_grid = QHBoxLayout()
        conn_grid.setSpacing(12)

        name_col = QVBoxLayout()
        name_col.addWidget(QLabel("Device Name"))
        self.device_name_input = QLineEdit(self.cfg["device_name"])
        name_col.addWidget(self.device_name_input)
        conn_grid.addLayout(name_col)

        timeout_col = QVBoxLayout()
        timeout_col.addWidget(QLabel("Scan Timeout (s)"))
        self.scan_timeout_input = QSpinBox()
        self.scan_timeout_input.setRange(5, 60)
        self.scan_timeout_input.setValue(self.cfg["scan_timeout"])
        timeout_col.addWidget(self.scan_timeout_input)
        conn_grid.addLayout(timeout_col)

        delay_col = QVBoxLayout()
        delay_col.addWidget(QLabel("Reconnect Delay (s)"))
        self.reconnect_delay_input = QSpinBox()
        self.reconnect_delay_input.setRange(1, 30)
        self.reconnect_delay_input.setValue(self.cfg["reconnect_delay"])
        delay_col.addWidget(self.reconnect_delay_input)
        conn_grid.addLayout(delay_col)

        settings_layout.addLayout(conn_grid)

        self.auto_reconnect_cb = QCheckBox("Auto Reconnect")
        self.auto_reconnect_cb.setChecked(self.cfg["auto_reconnect"])
        settings_layout.addWidget(self.auto_reconnect_cb)

        settings_layout.addStretch()

        # Save / Reset buttons
        save_row = QHBoxLayout()
        save_row.addStretch()

        reset_btn = QPushButton("Reset Defaults")
        reset_btn.setObjectName("secondaryBtn")
        reset_btn.clicked.connect(self._reset_defaults)
        save_row.addWidget(reset_btn)

        save_btn = QPushButton("Save")
        save_btn.setObjectName("primaryBtn")
        save_btn.clicked.connect(self._save_settings)
        save_row.addWidget(save_btn)

        settings_layout.addLayout(save_row)

        self.tabs.addTab(settings_widget, "Settings")

        # Tray note
        tray_note = QLabel("Close button minimizes to system tray. Right-click tray icon to quit.")
        tray_note.setStyleSheet("font-size: 11px; color: #666; border-left: 3px solid #ff6b35; padding-left: 8px;")
        root.addWidget(tray_note)

    # ---- system tray ----

    def _setup_tray(self):
        self.tray_icon = QSystemTrayIcon(self)
        self._icon_red = _make_colored_icon("#e74c3c")
        self._icon_green = _make_colored_icon("#2ecc71")
        self._icon_orange = _make_colored_icon("#f39c12")
        self.tray_icon.setIcon(self._icon_red)

        tray_menu = QMenu()
        self.show_action = QAction("Show Window", self)
        self.show_action.triggered.connect(self._show_window)
        tray_menu.addAction(self.show_action)

        self.tray_connect_action = QAction("Connect", self)
        self.tray_connect_action.triggered.connect(self._on_tray_toggle)
        tray_menu.addAction(self.tray_connect_action)

        tray_menu.addSeparator()

        quit_action = QAction("Quit", self)
        quit_action.triggered.connect(self._quit_app)
        tray_menu.addAction(quit_action)

        self.tray_icon.setContextMenu(tray_menu)
        self.tray_icon.activated.connect(self._on_tray_activated)
        self.tray_icon.show()

    def _on_tray_activated(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.DoubleClick:
            self._show_window()

    def _show_window(self):
        self.showNormal()
        self.activateWindow()

    def _on_tray_toggle(self):
        if self._state == "disconnected":
            self._on_connect_clicked()
        else:
            self._on_disconnect_clicked()

    def _quit_app(self):
        self.core.stop()
        self.tray_icon.hide()
        QApplication.quit()

    def closeEvent(self, event):
        event.ignore()
        self.hide()

    # ---- UI state management ----

    def _update_ui_state(self, state: str):
        self._state = state

        if state == "disconnected":
            self.status_dot.setStyleSheet(
                "background-color: #e74c3c; border-radius: 6px;"
            )
            self.status_text.setText("Disconnected")
            self.connect_btn.setVisible(True)
            self.connect_btn.setEnabled(True)
            self.disconnect_btn.setVisible(False)
            self.cancel_btn.setVisible(False)
            self.clear_btn.setVisible(True)
            self.devices_container.setVisible(False)
            self.tray_icon.setIcon(self._icon_red)
            self.tray_connect_action.setText("Connect")
            self._clear_device_cards()

        elif state == "scanning":
            self.status_dot.setStyleSheet(
                "background-color: #f39c12; border-radius: 6px;"
            )
            self.status_text.setText("Scanning...")
            self.connect_btn.setVisible(True)
            self.connect_btn.setEnabled(False)
            self.disconnect_btn.setVisible(False)
            self.cancel_btn.setVisible(True)
            self.clear_btn.setVisible(False)
            self.tray_icon.setIcon(self._icon_orange)
            self.tray_connect_action.setText("Cancel")

        elif state == "connected":
            self.status_dot.setStyleSheet(
                "background-color: #2ecc71; border-radius: 6px;"
            )
            self.connect_btn.setVisible(False)
            self.disconnect_btn.setVisible(True)
            self.cancel_btn.setVisible(False)
            self.clear_btn.setVisible(True)
            self.tray_icon.setIcon(self._icon_green)
            self.tray_connect_action.setText("Disconnect")

    def _update_device_count(self):
        count = len(self._device_cards)
        if count > 0:
            self.status_text.setText(f"Connected \u2014 {count} device{'s' if count != 1 else ''}")
            self.devices_container.setVisible(True)
        else:
            self.devices_container.setVisible(False)

    # ---- slots ----

    def _on_connect_clicked(self):
        self.cfg = load_config()
        self.core = ZwooshCore(self.cfg)
        self._setup_core_callbacks()
        self.core.start()

    def _on_disconnect_clicked(self):
        self.core.stop()

    def _on_log(self, ts: str, level: str, source: str, message: str):
        color_map = {
            "info": "#3498db",
            "ok": "#2ecc71",
            "warn": "#f39c12",
            "error": "#e74c3c",
            "shift": "#ff6b35",
        }
        color = color_map.get(level, "#e0e0e0")
        weight = "font-weight:600;" if level == "shift" else ""
        html = (
            f'<span style="color:#555">{ts}</span> '
            f'<span style="color:{color};{weight}">[{source}]</span> '
            f'<span style="color:#ccc;{weight}">{message}</span>'
        )
        self.log_text.append(html)
        self.log_text.moveCursor(QTextCursor.MoveOperation.End)

    def _on_connected(self, label: str, address: str, battery: object):
        bat = battery if isinstance(battery, int) else None
        card = DeviceCard(label, address, bat)
        self._device_cards[label] = card
        self.devices_layout.addWidget(card)
        self._update_device_count()

    def _on_disconnected(self, label: str):
        card = self._device_cards.pop(label, None)
        if card:
            self.devices_layout.removeWidget(card)
            card.deleteLater()
        self._update_device_count()

    def _clear_device_cards(self):
        for card in self._device_cards.values():
            self.devices_layout.removeWidget(card)
            card.deleteLater()
        self._device_cards.clear()

    def _clear_logs(self):
        self.log_text.clear()

    # ---- settings ----

    def _save_settings(self):
        new_cfg = {
            "device_name": self.device_name_input.text(),
            "shift_up_key": self.shift_up_input.text(),
            "shift_down_key": self.shift_down_input.text(),
            "scan_timeout": self.scan_timeout_input.value(),
            "reconnect_delay": self.reconnect_delay_input.value(),
            "auto_reconnect": self.auto_reconnect_cb.isChecked(),
        }
        self.cfg = {**DEFAULT_CONFIG, **new_cfg}
        save_config(self.cfg)
        self.core.reload_config(new_cfg)
        self._on_log(
            time.strftime("%H:%M:%S"),
            "ok", "Settings", "Configuration saved"
        )

    def _reset_defaults(self):
        self.device_name_input.setText(DEFAULT_CONFIG["device_name"])
        self.shift_up_input.setText(DEFAULT_CONFIG["shift_up_key"])
        self.shift_down_input.setText(DEFAULT_CONFIG["shift_down_key"])
        self.scan_timeout_input.setValue(DEFAULT_CONFIG["scan_timeout"])
        self.reconnect_delay_input.setValue(DEFAULT_CONFIG["reconnect_delay"])
        self.auto_reconnect_cb.setChecked(DEFAULT_CONFIG["auto_reconnect"])


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    app.setStyleSheet(DARK_STYLE)

    window = ZwooshWindow()
    window.show()

    sys.exit(app.exec())


if __name__ == "__main__":
    main()
