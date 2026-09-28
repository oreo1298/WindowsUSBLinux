"""Execution context for the helper: events, cancellation, subprocesses, privileges."""

from __future__ import annotations

import contextlib
import ctypes
import ctypes.util
import json
import os
import pwd
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from typing import Callable

SAFE_PATH = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
RUN_DIR = "/run/rufux"


class HelperError(Exception):
    """A failure that should be reported to the user as-is."""


class Cancelled(Exception):
    pass


class Emitter:
    """Thread-safe JSON-lines writer with progress throttling."""

    def __init__(self, stream=None):
        self.stream = stream if stream is not None else sys.stdout
        self.lock = threading.Lock()
        self._last_progress = 0.0
        self.log_lines: list[str] = []

    def emit(self, **event) -> None:
        line = json.dumps(event, ensure_ascii=False)
        with self.lock:
            try:
                self.stream.write(line + "\n")
                self.stream.flush()
            except (BrokenPipeError, ValueError, OSError):
                pass

    def log(self, msg: str) -> None:
        self.log_lines.append(msg)
        self.emit(t="log", msg=msg)

    def status(self, msg: str) -> None:
        self.emit(t="status", msg=msg)

    def progress(self, phase: str, done: int, total: int, msg: str = "", force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_progress < 0.2 and done < total:
            return
        self._last_progress = now
        self.emit(t="progress", phase=phase, done=int(done), total=int(max(total, 0)), msg=msg)


class Context:
    def __init__(self, emitter: Emitter | None = None, cancel: threading.Event | None = None,
                 user_uid: int | None = None, user_gid: int | None = None):
        self.out = emitter or Emitter()
        self.cancel_event = cancel or threading.Event()
        self.user_uid = user_uid
        self.user_gid = user_gid
        self._cleanups: list[tuple[str, Callable[[], None]]] = []
        self._procs: set[subprocess.Popen] = set()
        self._proc_lock = threading.Lock()
        self.env = {"PATH": SAFE_PATH, "LC_ALL": "C", "LANG": "C", "HOME": "/root"}

    # -- reporting ---------------------------------------------------------

    def log(self, msg: str) -> None:
        self.out.log(msg)

    def status(self, msg: str) -> None:
        self.out.status(msg)
        self.out.log(msg)

    def progress(self, phase: str, done: int, total: int, msg: str = "", force: bool = False) -> None:
        self.out.progress(phase, done, total, msg, force)

    # -- cancellation --------------------------------------------------------

    @property
    def cancelled(self) -> bool:
        return self.cancel_event.is_set()

    def check(self) -> None:
        if self.cancel_event.is_set():
            raise Cancelled()

    def cancel(self) -> None:
        self.cancel_event.set()
        with self._proc_lock:
            procs = list(self._procs)
        for p in procs:
            with contextlib.suppress(OSError):
                p.terminate()

    # -- cleanup stack -------------------------------------------------------

    def add_cleanup(self, name: str, fn: Callable[[], None]) -> Callable[[], None]:
        entry = (name, fn)
        self._cleanups.append(entry)

        def remove() -> None:
            with contextlib.suppress(ValueError):
                self._cleanups.remove(entry)

        return remove

    def run_cleanups(self) -> None:
        while self._cleanups:
            name, fn = self._cleanups.pop()
            try:
                fn()
            except Exception as exc:  # noqa: BLE001 - cleanup must go on
                self.log(f"Cleanup '{name}' failed: {exc}")

    # -- subprocesses ----------------------------------------------------------

    def tool(self, *names: str, package: str | None = None) -> str:
        for n in names:
            path = shutil.which(n, path=SAFE_PATH)
            if path:
                return path
        hint = f" (install it with: sudo pacman -S {package})" if package else ""
        raise HelperError(f"Required program '{names[0]}' was not found{hint}")

    def run(self, cmd: list[str], *, input: bytes | str | None = None, check: bool = True,
            timeout: float | None = 600, quiet: bool = False) -> subprocess.CompletedProcess:
        self.check()
        if not quiet:
            self.log("$ " + " ".join(_quote(c) for c in cmd))
        if isinstance(input, str):
            input = input.encode()
        try:
            proc = subprocess.run(cmd, input=input, capture_output=True, timeout=timeout,
                                  env=self.env, check=False)
        except FileNotFoundError as exc:
            raise HelperError(f"Program not found: {cmd[0]}") from exc
        except subprocess.TimeoutExpired as exc:
            raise HelperError(f"'{os.path.basename(cmd[0])}' timed out") from exc
        out = (proc.stdout or b"").decode("utf-8", "replace")
        err = (proc.stderr or b"").decode("utf-8", "replace")
        if not quiet or proc.returncode != 0:
            for line in (out + err).splitlines()[-40:]:
                if line.strip():
                    self.log("  " + line.rstrip())
        if check and proc.returncode != 0:
            tail = (err.strip() or out.strip()).splitlines()[-3:]
            raise HelperError(f"'{os.path.basename(cmd[0])}' failed (exit code {proc.returncode})"
                              + (": " + " ".join(tail) if tail else ""))
        return proc

    def run_progress(self, cmd: list[str], on_line: Callable[[str], None], *,
                     check: bool = True, stdin=None) -> int:
        """Run a long command, feeding each output line (split on CR/LF) to on_line."""
        self.check()
        self.log("$ " + " ".join(_quote(c) for c in cmd))
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    stdin=stdin if stdin is not None else subprocess.DEVNULL,
                                    env=self.env)
        except FileNotFoundError as exc:
            raise HelperError(f"Program not found: {cmd[0]}") from exc
        with self._proc_lock:
            self._procs.add(proc)
        tail: list[str] = []
        try:
            assert proc.stdout is not None
            buf = b""
            while True:
                chunk = proc.stdout.read1(65536) if hasattr(proc.stdout, "read1") else proc.stdout.read(4096)
                if not chunk:
                    break
                buf += chunk
                parts = re.split(rb"[\r\n]", buf)
                buf = parts.pop()
                for p in parts:
                    line = p.decode("utf-8", "replace").strip()
                    if line:
                        on_line(line)
                        tail.append(line)
                        del tail[:-20]
            if buf.strip():
                line = buf.decode("utf-8", "replace").strip()
                on_line(line)
                tail.append(line)
            rc = proc.wait()
        finally:
            with self._proc_lock:
                self._procs.discard(proc)
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        self.check()
        if check and rc != 0:
            for line in tail[-10:]:
                self.log("  " + line)
            raise HelperError(f"'{os.path.basename(cmd[0])}' failed (exit code {rc})"
                              + (": " + tail[-1] if tail else ""))
        return rc

    # -- privileges --------------------------------------------------------------

    @contextlib.contextmanager
    def as_user(self):
        """Temporarily drop to the invoking user's credentials (for opening their files)."""
        uid, gid = self.user_uid, self.user_gid
        if uid is None or uid == 0 or os.geteuid() != 0:
            yield
            return
        try:
            name = pwd.getpwuid(uid).pw_name
            groups = os.getgrouplist(name, gid if gid is not None else uid)
        except KeyError:
            groups = [gid if gid is not None else uid]
        old_groups = os.getgroups()
        os.setgroups(groups)
        os.setegid(gid if gid is not None else uid)
        os.seteuid(uid)
        try:
            yield
        finally:
            os.seteuid(0)
            os.setegid(0)
            os.setgroups(old_groups)

    def open_user_file(self, path: str, flags: int = os.O_RDONLY, mode: int = 0o644) -> int:
        with self.as_user():
            try:
                return os.open(path, flags | os.O_CLOEXEC, mode)
            except OSError as exc:
                raise HelperError(f"Cannot open '{path}': {exc.strerror}") from exc

    # -- temp dirs ------------------------------------------------------------------

    def make_temp_dir(self, prefix: str = "tmp-") -> str:
        os.makedirs(RUN_DIR, mode=0o700, exist_ok=True)
        os.chmod(RUN_DIR, 0o700)
        path = tempfile.mkdtemp(prefix=prefix, dir=RUN_DIR)
        self.add_cleanup(f"rmdir {path}", lambda: _rmdir_quiet(path))
        return path


def _rmdir_quiet(path: str) -> None:
    with contextlib.suppress(OSError):
        os.rmdir(path)


def _quote(s: str) -> str:
    if s and all(c.isalnum() or c in "-_./=:,+@%" for c in s):
        return s
    return "'" + s.replace("'", "'\\''") + "'"


_libc = None


def syncfs(path_or_fd) -> None:
    """Flush one file system (falls back to a global sync)."""
    global _libc
    fd = None
    try:
        if _libc is None:
            _libc = ctypes.CDLL(ctypes.util.find_library("c") or "libc.so.6", use_errno=True)
        if isinstance(path_or_fd, int):
            if _libc.syncfs(path_or_fd) == 0:
                return
        else:
            fd = os.open(path_or_fd, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
            if _libc.syncfs(fd) == 0:
                return
    except (OSError, AttributeError):
        pass
    finally:
        if fd is not None:
            os.close(fd)
    os.sync()
