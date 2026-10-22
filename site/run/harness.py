"""Run an unmodified Fieldwork command-line tool inside Pyodide.

Each tool is a single-file script, so it is compiled once and executed as
__main__ with the requested argv. Three shims cover what a browser withholds
from Python: hard links, sockets, and the FFmpeg binaries. Each is installed
per run, named on the page, and never changes what a tool decides.
"""
import contextlib
import email.message
import io
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

PROGRAM = {}
REAL = {"which": shutil.which, "run": subprocess.run, "opener": urllib.request.build_opener}


def install(name, source):
    PROGRAM[name] = compile(source, name + ".py", "exec")


def write_text(path, text):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")


def write_bytes(path, data):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(bytes(data.to_py()) if hasattr(data, "to_py") else bytes(data))


def run(name, argv_json):
    namespace = {"__name__": "__main__", "__file__": "/cli/" + name + ".py"}
    out, err = io.StringIO(), io.StringIO()
    saved = sys.argv
    sys.argv = [name, *json.loads(argv_json)]
    status = 0
    started = time.perf_counter()
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            exec(PROGRAM[name], namespace)
    except SystemExit as exit_request:
        code = exit_request.code
        status = code if isinstance(code, int) else (0 if code is None else 1)
    except BaseException:  # reported on the page as a failed run, never hidden
        import traceback

        err.write(traceback.format_exc())
        status = 70
    finally:
        sys.argv = saved
    return json.dumps({
        "exit": status,
        "stdout": out.getvalue(),
        "stderr": err.getvalue(),
        "ms": round((time.perf_counter() - started) * 1000),
    })


def install_link_fallback():
    """The in-memory browser filesystem has no hard links.

    The tools publish finished output by linking a temporary file into place, so
    without this they cannot write at all. An exclusive create plus a byte copy
    keeps the no-overwrite guarantee. It does not share an inode, so a tool that
    checks file identity after linking still refuses to run here, which is why
    Safe Tidy stops at its plan.
    """
    if hasattr(os, "link"):
        return

    def link(source, target, follow_symlinks=True, **_):
        data = Path(source).read_bytes()
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)

    os.link = link


class Response:
    """The part of http.client.HTTPResponse that the tools read."""

    def __init__(self, status, headers, body):
        self.status = status
        self.headers = headers
        self._body = body
        self._offset = 0

    def read(self, amount=None):
        end = len(self._body) if amount is None else min(len(self._body), self._offset + amount)
        chunk = self._body[self._offset:end]
        self._offset = end
        return chunk

    def close(self):
        self._body = b""


class XhrOpener:
    """A blocking XMLHttpRequest stands in for the sockets Python cannot open.

    Content-encoding and content-length are dropped because the browser has
    already decoded the body; reporting them would describe a response Python
    never saw.
    """

    def open(self, request, timeout=None):
        import urllib.error
        from js import XMLHttpRequest

        xhr = XMLHttpRequest.new()
        xhr.open(request.get_method(), request.full_url, False)
        xhr.overrideMimeType("text/plain; charset=x-user-defined")
        try:
            xhr.send(None)
        except Exception as failure:
            raise urllib.error.URLError(str(failure)) from failure
        headers = email.message.Message()
        for line in (xhr.getAllResponseHeaders() or "").splitlines():
            name, separator, value = line.partition(":")
            if separator and name.strip().lower() not in {"content-encoding", "content-length"}:
                headers[name.strip()] = value.strip()
        body = bytes(ord(character) & 0xFF for character in xhr.responseText or "")
        status = int(xhr.status)
        if status >= 400:
            raise urllib.error.HTTPError(request.full_url, status, xhr.statusText, headers, None)
        return Response(status, headers, body)


def prepare(recordings_json="{}", network=False):
    """Install exactly the shims one run needs and undo the others."""
    recorded = json.loads(recordings_json)

    def which(command, *args, **kwargs):
        if command in recorded:
            return "/replayed/" + command
        return REAL["which"](command, *args, **kwargs)

    def replay(command, *args, **kwargs):
        name = os.path.basename(str(command[0]))
        saved = recorded.get(name)
        if saved is None:
            return REAL["run"](command, *args, **kwargs)
        return subprocess.CompletedProcess(command, saved["returncode"], saved["stdout"], saved["stderr"])

    shutil.which = which if recorded else REAL["which"]
    subprocess.run = replay if recorded else REAL["run"]
    urllib.request.build_opener = (lambda *handlers: XhrOpener()) if network else REAL["opener"]
