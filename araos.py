"""
ARAOS - BETA vers. 037
    Featuries:
 - system monitoring (cpu, memory and disc, network coming soon )
 - Automatic execution of remediation scripts
 - PyQt6 graphical interface with tabs: Dashboard, Code Creator, Architecture.

"""


#!/usr/bin/env python3
import os
import sys
import time
import json
import tempfile
import subprocess
from pathlib import Path
from collections import deque
from typing import Tuple

import psutil
import pyautogui
import winshell
import shutil


from PyQt6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QProgressBar, QStatusBar, QTabWidget, QTextEdit, QPushButton,
    QFileDialog, QSplitter, QLabel, QListWidget, QMessageBox, QCheckBox,
    QLineEdit
)
from PyQt6.QtGui import QAction, QPalette, QColor, QFont, QPainter, QMovie
from PyQt6.QtCore import (
    QTimer, Qt, QThread, pyqtSignal, QElapsedTimer, QObject, pyqtSlot
)
from matplotlib.backends.backend_qt5agg import FigureCanvasQTAgg as BaseCanvas
from matplotlib.figure import Figure

# — Templates for auto-remediation scripts —
class CodeGenerator:
    TEMPLATES = {
        "high_cpu": """
import psutil
for p in psutil.process_iter(['name','cpu_percent']):
    if p.info['name']=="{process}" and p.info['cpu_percent']>{th}:
        p.kill()
print("Killed high-CPU process")
""",
        "high_memory": """
import psutil
for p in psutil.process_iter(['name','memory_info']):
    if p.info['name']=="{process}" and p.info['memory_info'].rss/1024/1024>{mb}:
        p.kill()
print("Killed high-memory process")
""",
        "cleanup_temp": r"""
import shutil
from pathlib import Path
t = Path(r"{path}")
for f in t.iterdir():
    try:
        if f.is_file(): f.unlink()
        else: shutil.rmtree(f)
    except: pass
print("Temp folder cleaned")
""",
        "cleanup_recycle": """
import winshell
winshell.recycle_bin().empty(confirm=False, show_progress=False, sound=False)
print("Recycle bin emptied")
"""
    }

    def generate(self, kind: str, **ctx) -> str:
        tpl = self.TEMPLATES[kind]
        if "path" in ctx:
            ctx["path"] = Path(ctx["path"]).as_posix()
        return tpl.format(**ctx)


# — Executes generated scripts safely —
class AutoExecutor:
    def __init__(self, work_dir: Path):
        self.work_dir = work_dir
        self.work_dir.mkdir(exist_ok=True, parents=True)

    def run(self, code: str) -> Tuple[bool, str]:
        compile(code, "<ara_auto>", "exec")
        with tempfile.NamedTemporaryFile(
            dir=self.work_dir, suffix=".py", delete=False,
            mode="w", encoding="utf-8"
        ) as tmp:
            tmp.write(code)
            path = tmp.name
        proc = subprocess.run([sys.executable, path], capture_output=True, text=True)
        out = proc.stdout or ""
        err = proc.stderr or ""
        return proc.returncode == 0, out + err


# — Background worker for Architecture Mode, self-healing —
class MonitorWorker(QObject):
    action_ready = pyqtSignal(str, str)
    error_signal = pyqtSignal(str)

    def __init__(self):
        super().__init__()
        self.gen = CodeGenerator()
        self._last_temp = 0
        self._last_rec = 0
        self.timer = QTimer(self)
        self.timer.timeout.connect(self._safe_check)
        self.timer.start(5000)

    def _safe_check(self):
        try:
            self.check()
        except Exception as e:
            self.error_signal.emit(str(e))
            self.timer.stop()
            QTimer.singleShot(60000, self.timer.start)

    def check(self):
        now = time.time()
        # high CPU
        for p in psutil.process_iter(['name','cpu_percent']):
            if p.info['name']=="chrome.exe" and p.info['cpu_percent']>80:
                code = self.gen.generate("high_cpu", process="chrome.exe", th=80)
                self.action_ready.emit("Kill chrome.exe (CPU>80%)", code)
        # high Memory
        for p in psutil.process_iter(['name','memory_info']):
            mb = p.info['memory_info'].rss / 1024 / 1024
            if p.info['name'] and mb > 500:
                desc = f"Kill {p.info['name']} (Mem>500MB)"
                code = self.gen.generate("high_memory", process=p.info['name'], mb=500)
                self.action_ready.emit(desc, code)
        # cleanup temp every 60s
        if now - self._last_temp > 60:
            code = self.gen.generate("cleanup_temp", path=tempfile.gettempdir())
            self.action_ready.emit("Cleanup Temp Folder", code)
            self._last_temp = now
        # empty recycle every 300s
        if now - self._last_rec > 300:
            code = self.gen.generate("cleanup_recycle")
            self.action_ready.emit("Empty Recycle Bin", code)
            self._last_rec = now


# — Canvas with FPS overlay for Dashboard charts —
class FPSCanvas(BaseCanvas):
    def __init__(self, figure, parent=None):
        super().__init__(figure)
        self._last = None
        self._fps = 0.0
        self._count = 0
        self._sum_dt = 0

    def paintEvent(self, event):
        try:
            timer = QElapsedTimer(); timer.start()
            super().paintEvent(event)
            elapsed = timer.elapsed()
            if self._last is not None:
                dt = elapsed - self._last
                self._sum_dt += dt
                self._count += 1
                if self._count >= 10:
                    self._fps = 1000 * self._count / self._sum_dt
                    self._sum_dt = 0
                    self._count = 0
            self._last = elapsed

            painter = QPainter(self)
            painter.setPen(QColor('white'))
            painter.drawText(10, 20, f"FPS: {self._fps:.1f}")
            painter.end()
        except Exception as e:
            print("FPSCanvas error:", e)

# — Main application window integrating all tabs —
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("ARA")
        self.resize(1000, 800)

        self._dash_ok = True
        self._arch_ok = True

        self.executor = AutoExecutor(Path(tempfile.gettempdir()) / "ara_auto")
        self.arch_worker = None

        sys.excepthook = self._global_exception_hook

        self._init_ui()
        self._init_data()
        self._init_timers()

    def _global_exception_hook(self, exctype, value, tb):
        self._handle_error('dashboard', f"{exctype.__name__}: {value}")

    def _init_ui(self):
        menu = self.menuBar().addMenu("Plugins")
        menu.addAction(QAction("Reload Plugins", self))

        self.tabs = QTabWidget()
        self.setCentralWidget(self.tabs)

        d = QWidget(); self._build_dashboard(d);    self.tabs.addTab(d, "Dashboard")
        c = QWidget(); self._build_code_creator(c); self.tabs.addTab(c, "Code Creator")
        a = QWidget(); self._build_architecture(a);self.tabs.addTab(a, "Architecture")

        self.setStatusBar(QStatusBar(self))
        self._apply_dark_theme()

    def _apply_dark_theme(self):
        self.setStyleSheet("""
            QMainWindow { background: #222; }
            QMenuBar, QMenu { background: #333; color: #ddd; }
            QProgressBar { background: #444; color: #fff; border:1px solid #555; }
            QProgressBar::chunk { background: #00c853; }
            QPushButton { background: #444; color: #ddd; border:1px solid #555; }
            QTextEdit, QListWidget, QCheckBox, QLineEdit { background: #1e1e1e; color: #eee; }
        """)

    def _build_dashboard(self, w: QWidget):
        L = QVBoxLayout(w); L.setContentsMargins(10,10,10,10)
        for attr, fmt in (("cpu_bar","CPU: %p%"),("mem_bar","Memória: %p%"),("disk_bar","Disco: %p%")):
            bar = QProgressBar(); bar.setFormat(fmt); bar.setFixedHeight(24)
            setattr(self, attr, bar); L.addWidget(bar)
        fig = Figure(figsize=(12,9), tight_layout=True)
        fig.patch.set_facecolor('#2b2b2b')
        self.canvas = FPSCanvas(fig)
        self.axes = [fig.add_subplot(2,2,i+1) for i in range(4)]
        for ax in self.axes:
            ax.set_facecolor('#313131'); ax.tick_params(colors='white'); ax.title.set_color('white')
        L.addWidget(self.canvas, stretch=2)
        # Slow animation
        anim = QLabel(); anim.setFixedHeight(150)
        movie = QMovie("anim.gif"); movie.setSpeed(30); anim.setMovie(movie); movie.start()
        L.addWidget(anim, stretch=1)
        info = QHBoxLayout()
        self.net_label = QLabel("Rede: 0/0 KB/s"); self.uptime_label = QLabel("Uptime: 0s")
        for lbl in (self.net_label, self.uptime_label): lbl.setStyleSheet("color:white;")
        info.addWidget(self.net_label); info.addStretch(); info.addWidget(self.uptime_label)
        L.addLayout(info)

    def _build_code_creator(self, w: QWidget):
        L = QVBoxLayout(w); L.setContentsMargins(10,10,10,10); L.setSpacing(6)
        sp = QSplitter(Qt.Orientation.Horizontal); L.addWidget(sp, stretch=1)
        # Editor
        edw = QWidget(); edl = QVBoxLayout(edw)
        self.editor = QTextEdit(); self.editor.setFont(QFont("Consolas",11))
        edl.addWidget(self.editor, stretch=1)
        hl = QHBoxLayout()
        for name, slot in (("New", self._new_file),("Open", self._open_file),
                           ("Save", self._save_file),("Run", self._run_code)):
            b = QPushButton(name); b.clicked.connect(slot); b.setFixedHeight(28); hl.addWidget(b)
        edl.addLayout(hl); sp.addWidget(edw)
        # Console
        cw = QWidget(); cl = QVBoxLayout(cw)
        self.console = QTextEdit(); self.console.setReadOnly(True); self.console.setFont(QFont("Consolas",11))
        cl.addWidget(self.console, stretch=1); sp.addWidget(cw); sp.setSizes([600,400])

    def _build_architecture(self, w: QWidget):
        L = QVBoxLayout(w); L.setContentsMargins(10,10,10,10); L.setSpacing(6)
        self.chk = QCheckBox("Enable Architecture Mode")
        self.chk.stateChanged.connect(self._toggle_arch)
        L.addWidget(self.chk)
        self.pending = QListWidget()
        self.pending.currentRowChanged.connect(self._show_script)
        L.addWidget(self.pending, stretch=2)
        self.script_view = QTextEdit(); self.script_view.setReadOnly(True)
        L.addWidget(self.script_view, stretch=1)
        btns = QHBoxLayout()
        self.btn_confirm = QPushButton("Confirm"); self.btn_confirm.clicked.connect(self._confirm)
        self.btn_reject  = QPushButton("Reject");  self.btn_reject.clicked.connect(self._reject)
        btns.addWidget(self.btn_confirm); btns.addWidget(self.btn_reject); L.addLayout(btns)

    def _init_data(self):
        self.history    = 60
        self.cpu_h      = deque([0]*self.history, maxlen=self.history)
        self.mem_h      = deque([0]*self.history, maxlen=self.history)
        self.disk_h     = deque([0]*self.history, maxlen=self.history)
        self.net_sent_h = deque([0]*self.history, maxlen=self.history)
        self.net_recv_h = deque([0]*self.history, maxlen=self.history)
        self.start      = time.time()
        self.prev_net   = psutil.net_io_counters()

    def _init_timers(self):
        self.dash_timer = QTimer(self)
        self.dash_timer.timeout.connect(self._protected_update)
        self.dash_timer.start(1000)

    def _protected_update(self):
        if not self._dash_ok:
            return
        try:
            self._update()
        except Exception as e:
            self._handle_error('dashboard', str(e))
            self.dash_timer.stop()
            QTimer.singleShot(60000, self.dash_timer.start)

    def _update(self):
        cpu = psutil.cpu_percent(); mem = psutil.virtual_memory().percent
        disk = psutil.disk_usage(str(Path.home())).percent
        net  = psutil.net_io_counters()
        sent = (net.bytes_sent - self.prev_net.bytes_sent)/1024
        recv = (net.bytes_recv - self.prev_net.bytes_recv)/1024
        self.prev_net = net
        up   = int(time.time() - self.start)

        self.cpu_bar.setValue(int(cpu))
        self.mem_bar.setValue(int(mem))
        self.disk_bar.setValue(int(disk))
        self.net_label.setText(f"Rede: {sent:.1f}/{recv:.1f} KB/s")
        self.uptime_label.setText(f"Uptime: {up}s")

        self.cpu_h.append(cpu); self.mem_h.append(mem); self.disk_h.append(disk)
        self.net_sent_h.append(sent); self.net_recv_h.append(recv)

        x = list(range(-self.history+1,1))
        titles = ["CPU (%)","Memória (%)","Disco (%)","Rede (KB/s)"]
        for ax, hist, title in zip(self.axes,
                                   [self.cpu_h,self.mem_h,self.disk_h,None],
                                   titles):
            ax.clear(); ax.set_facecolor('#313131')
            ax.tick_params(colors='white'); ax.title.set_color('white')
            if title!="Rede (KB/s)":
                ax.plot(x, list(hist), linewidth=1)
            else:
                ax.plot(x, list(self.net_sent_h), label="Sent", linewidth=1)
                ax.plot(x, list(self.net_recv_h), label="Recv", linewidth=1)
                ax.legend(loc="upper right", facecolor='#313131', edgecolor='white', labelcolor='white')
            ax.set_title(title)
        self.canvas.draw()

    def _toggle_arch(self, state):
        if not self._arch_ok:
            return
        if state == Qt.CheckState.Checked.value:
            self.pending.clear(); self.script_view.clear()
            if not self.arch_worker:
                self.arch_worker = MonitorWorker()
                self.arch_worker.action_ready.connect(self._enqueue)
                self.arch_worker.error_signal.connect(lambda e: self._handle_error('architecture', e))
                self.arch_worker.moveToThread(QThread(self))
                self.arch_worker.thread().start()
        else:
            if self.arch_worker:
                self.arch_worker.timer.stop()

    def _enqueue(self, desc: str, code: str):
        item = QListWidget.QListWidgetItem(desc)
        item.setData(Qt.ItemDataRole.UserRole, code)
        self.pending.addItem(item)

    def _show_script(self, idx):
        itm = self.pending.currentItem()
        self.script_view.setPlainText(itm.data(Qt.ItemDataRole.UserRole) if itm else "")

    def _confirm(self):
        itm = self.pending.currentItem()
        if not itm:
            return
        name = itm.text()
        code = itm.data(Qt.ItemDataRole.UserRole)
        ok, out = self.executor.run(code)
        QMessageBox.information(self, "Architecture", f"{name}: {'OK' if ok else 'FAIL'}\n\n{out}")
        self.pending.takeItem(self.pending.row(itm))

    def _reject(self):
        idx = self.pending.currentRow()
        if idx >= 0:
            self.pending.takeItem(idx)

    def _new_file(self):
        self.editor.clear(); self.console.clear()

    def _open_file(self):
        p, _ = QFileDialog.getOpenFileName(self, "Open Python File", "", "*.py")
        if p:
            text = Path(p).read_text(encoding='utf-8')
            self.editor.setPlainText(text)
            self.console.append(f"Opened: {p}")

    def _save_file(self):
        p, _ = QFileDialog.getSaveFileName(self, "Save Python File", "", "*.py")
        if p:
            Path(p).write_text(self.editor.toPlainText(), encoding='utf-8')
            self.console.append(f"Saved: {p}")

    def _run_code(self):
        code = self.editor.toPlainText(); self.console.clear()
        try:
            compile(code, '<ara>', 'exec')
        except Exception as e:
            self.console.append(f"Syntax Error:\n{e}")
            return
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False, encoding='utf-8') as tmp:
            tmp.write(code); tp = tmp.name
        res = subprocess.run([sys.executable, tp], capture_output=True, text=True, timeout=10)
        self.console.append(res.stdout or "")
        if res.stderr:
            self.console.append(f"Errors:\n{res.stderr}")

    def _handle_error(self, feature: str, msg: str):
        print(f"[{feature} ERROR] {msg}")
        self.statusBar().showMessage(f"{feature} in maintenance", 5000)
        if feature == 'dashboard':
            self._dash_ok = False
        if feature == 'architecture':
            self._arch_ok = False
            self.chk.setEnabled(False)

def main():
    app = QApplication(sys.argv)
    app.setStyle("Fusion")
    pal = QPalette()
    pal.setColor(QPalette.ColorRole.Window, QColor(30,30,30))
    pal.setColor(QPalette.ColorRole.WindowText, QColor(200,200,200))
    pal.setColor(QPalette.ColorRole.Base, QColor(25,25,25))
    pal.setColor(QPalette.ColorRole.AlternateBase, QColor(53,53,53))
    pal.setColor(QPalette.ColorRole.ToolTipBase, QColor(200,200,200))
    pal.setColor(QPalette.ColorRole.ToolTipText, QColor(200,200,200))
    pal.setColor(QPalette.ColorRole.Text, QColor(200,200,200))
    pal.setColor(QPalette.ColorRole.Button, QColor(53,53,53))
    pal.setColor(QPalette.ColorRole.ButtonText, QColor(200,200,200))
    app.setPalette(pal)

    win = MainWindow()
    win.show()
    sys.exit(app.exec())

if __name__ == "__main__":
    main()