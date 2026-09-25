"""Runs under LibreOffice's OWN bundled Python (has the uno/pyuno
bindings compiled in) — NOT the project's regular venv interpreter.
Invoked as a subprocess by diff_render.py, one process per comparison,
same isolation principle as render.py's per-conversion profile: a fresh
soffice listener + fresh profile dir per call, torn down afterward, so
concurrent diffs (or a diff overlapping a plain render) never share
state or a lock.

Document *comparison* (unlike render.py's plain format conversion) has
no `--convert-to`-style CLI shortcut — it's only exposed via the UNO
scripting API's ".uno:CompareDocuments" dispatch, which needs a live,
connected LibreOffice instance, not a one-shot subprocess call.

Usage: soffice_python.exe _diff_worker.py <new_docx> <old_docx> <dest_pdf>
Exit 0 on success. Exit 1 with a message on stderr otherwise.
"""
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("localhost", 0))
        return s.getsockname()[1]


def main():
    if len(sys.argv) != 4:
        print("usage: _diff_worker.py <new_docx> <old_docx> <dest_pdf>", file=sys.stderr)
        sys.exit(1)

    new_path, old_path, dest_pdf = (Path(a).resolve() for a in sys.argv[1:4])
    soffice_dir = Path(sys.executable).parent
    soffice_exe = soffice_dir / "soffice.exe"
    if not soffice_exe.exists():
        soffice_exe = soffice_dir / "soffice"

    port = _free_port()
    try:
        # The listener's own lock files live inside profile_dir, so it
        # must be fully terminated (releasing those locks) BEFORE
        # TemporaryDirectory's own __exit__ tries to delete it -- doing
        # that teardown in an inner try/finally, nested inside the `with`,
        # is what guarantees that ordering (an outer finally would run
        # too late, after the directory delete already failed on Windows
        # with the listener process still holding files open in it).
        with tempfile.TemporaryDirectory() as profile_dir:
            listener = subprocess.Popen(
                [str(soffice_exe), "--headless", "--norestore", "--invisible",
                 f"-env:UserInstallation=file:///{Path(profile_dir).as_posix()}",
                 f"--accept=socket,host=localhost,port={port};urp;"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            doc = None
            try:
                # Importing uno itself is fast; it's the listener that's
                # slow to come up, so the retry loop lives around the
                # *connection* attempt, not the import.
                import uno
                from com.sun.star.beans import PropertyValue

                def make_prop(name, value):
                    p = PropertyValue()
                    p.Name = name
                    p.Value = value
                    return p

                local_ctx = uno.getComponentContext()
                resolver = local_ctx.ServiceManager.createInstanceWithContext(
                    "com.sun.star.bridge.UnoUrlResolver", local_ctx
                )
                ctx = None
                last_exc = None
                deadline = time.monotonic() + 30
                while time.monotonic() < deadline:
                    try:
                        ctx = resolver.resolve(
                            f"uno:socket,host=localhost,port={port};urp;StarOffice.ComponentContext"
                        )
                        break
                    except Exception as exc:
                        last_exc = exc
                        time.sleep(0.5)
                if ctx is None:
                    raise RuntimeError(f"could not connect to soffice listener: {last_exc}")

                smgr = ctx.ServiceManager
                desktop = smgr.createInstanceWithContext("com.sun.star.frame.Desktop", ctx)

                new_url = uno.systemPathToFileUrl(str(new_path))
                old_url = uno.systemPathToFileUrl(str(old_path))
                out_url = uno.systemPathToFileUrl(str(dest_pdf))

                doc = desktop.loadComponentFromURL(new_url, "_blank", 0, (make_prop("Hidden", True),))

                frame = doc.getCurrentController().getFrame()
                dispatch_helper = smgr.createInstanceWithContext("com.sun.star.frame.DispatchHelper", ctx)
                dispatch_helper.executeDispatch(
                    frame, ".uno:CompareDocuments", "", 0, (make_prop("URL", old_url),)
                )

                dest_pdf.parent.mkdir(parents=True, exist_ok=True)
                doc.storeToURL(out_url, (make_prop("FilterName", "writer_pdf_Export"),))
                doc.close(False)
                doc = None

                if not dest_pdf.exists():
                    raise RuntimeError("compare/export completed but no PDF was produced")
            finally:
                if doc is not None:
                    try:
                        doc.close(False)
                    except Exception:
                        pass
                listener.terminate()
                try:
                    listener.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    listener.kill()
                    listener.wait(timeout=10)

        print(str(dest_pdf))
        sys.exit(0)

    except Exception as exc:
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
