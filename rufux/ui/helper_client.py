"""Runs the privileged helper through pkexec and turns its JSON events into signals."""

from __future__ import annotations

import json
import os
import shutil
import sys

from ..core.distro import install_hint
from .qt import QObject, QProcess, Signal

PACKAGE_PARENT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def helper_path() -> str:
    env = os.environ.get("RUFUX_HELPER")
    if env:
        return env
    return os.path.join(PACKAGE_PARENT, "bin", "rufux-helper")


def helper_command() -> tuple[str, list[str]]:
    helper = helper_path()
    if os.geteuid() == 0:
        return sys.executable, ["-I", helper]
    pkexec = shutil.which("pkexec")
    if pkexec is None:
        raise RuntimeError("pkexec (polkit) is required to write to drives. "
                           f"Install it with: {install_hint('polkit')}")
    if not os.access(helper, os.X_OK):
        raise RuntimeError(f"The helper program is missing or not executable: {helper}")
    return pkexec, [helper]


class HelperClient(QObject):
    log = Signal(str)
    status = Signal(str)
    # phase, done, total, message.  done/total are byte counts that easily exceed
    # 2 GiB, so they must not be declared as (32-bit C++) int.
    progress = Signal(str, object, object, str)
    finished = Signal(bool, bool, str)  # ok, cancelled, message

    def __init__(self, parent=None):
        super().__init__(parent)
        self.proc: QProcess | None = None
        self._buf = b""
        self._done = False
        self._authorized = False

    @property
    def running(self) -> bool:
        return self.proc is not None and self.proc.state() != QProcess.ProcessState.NotRunning

    def start(self, job: dict) -> None:
        program, args = helper_command()
        self._buf = b""
        self._done = False
        self._authorized = False
        self.proc = QProcess(self)
        self.proc.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        self.proc.readyReadStandardOutput.connect(self._on_stdout)
        self.proc.readyReadStandardError.connect(self._on_stderr)
        self.proc.finished.connect(self._on_finished)
        self.proc.errorOccurred.connect(self._on_error)
        self.log.emit(f"Starting helper: {program} {' '.join(args)}")
        self.proc.start(program, args)
        self.proc.write(json.dumps(job).encode("utf-8") + b"\n")

    def cancel(self) -> None:
        if self.running and self.proc is not None:
            self.proc.write(b"cancel\n")

    # -- process I/O ---------------------------------------------------------

    def _on_stdout(self) -> None:
        assert self.proc is not None
        self._buf += self.proc.readAllStandardOutput().data()
        while b"\n" in self._buf:
            line, self._buf = self._buf.split(b"\n", 1)
            if not line.strip():
                continue
            try:
                ev = json.loads(line.decode("utf-8", "replace"))
            except ValueError:
                self.log.emit(line.decode("utf-8", "replace"))
                continue
            self._dispatch(ev)

    def _dispatch(self, ev: dict) -> None:
        t = ev.get("t")
        if t == "hello":
            self._authorized = True
            self.log.emit(f"Helper {ev.get('version', '?')} started with administrator privileges")
        elif t == "log":
            self.log.emit(str(ev.get("msg", "")))
        elif t == "status":
            self.status.emit(str(ev.get("msg", "")))
        elif t == "progress":
            self.progress.emit(str(ev.get("phase", "")), int(ev.get("done", 0)), int(ev.get("total", 0)),
                               str(ev.get("msg", "")))
        elif t == "done":
            self._done = True
            self.finished.emit(bool(ev.get("ok")), bool(ev.get("cancelled")), str(ev.get("msg", "")))

    def _on_stderr(self) -> None:
        assert self.proc is not None
        text = self.proc.readAllStandardError().data().decode("utf-8", "replace")
        for line in text.splitlines():
            if line.strip():
                self.log.emit("[helper] " + line)

    def _on_finished(self, code: int, status) -> None:
        if self._done:
            return
        self._done = True
        if not self._authorized and code in (126, 127):
            msg = ("Authorization failed or was cancelled. Administrator rights are required to write "
                   "to the drive (a polkit authentication agent must be running).")
            self.finished.emit(False, True if code == 126 else False, msg)
            return
        self.finished.emit(False, False, f"The helper exited unexpectedly (exit code {code}). See the log.")

    def _on_error(self, error) -> None:
        if error == QProcess.ProcessError.FailedToStart and not self._done:
            self._done = True
            self.finished.emit(False, False, "Could not start the privileged helper (is pkexec installed?)")
