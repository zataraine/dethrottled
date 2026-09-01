"""Suppress third-party noise that we have checked and understood.

Two sources, one mechanism: both are written to file descriptor 2 by C or
Rust code that never passes through `sys.stderr`, so `contextlib.
redirect_stderr` cannot see them and no logging call can filter them.

## onnxruntime GPU discovery

onnxruntime 1.27 probes /sys/class/drm during MODULE IMPORT -- not session
creation -- and writes straight to file descriptor 2 when it finds no GPU
vendor file:

    [W:onnxruntime:Default, device_discovery.cc:283 GetGpuDevices] Failed to
    detect devices under "/sys/class/drm/card1"

On a CPU-only host the probe is correct, and falling back
to CPU is what we want. The message is noise printed on every invocation.

`SessionOptions.log_severity_level` cannot suppress it. That configures a
session, and this is emitted by a static logger built during import, before any
session exists -- which is why setting it in corpus.py did not work. There is no
environment variable for it in 1.27 either; the knob is not there.

So it is caught where it is written: at the file descriptor, around the first
import only. `contextlib.redirect_stderr` does not work here because the write
comes from C++ and never passes through `sys.stderr`.

This hides C-level stderr chatter during that one import. It does not hide
import failures -- those raise, and Python prints the traceback after the
descriptor is restored.
"""
import os
import sys


def load():
    """The onnxruntime module, imported quietly the first time."""
    module = sys.modules.get("onnxruntime")
    if module is not None:
        return module                  # already imported; it has had its say

    saved = None
    try:
        sys.stderr.flush()
        saved = os.dup(2)
        devnull = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(devnull, 2)
        finally:
            os.close(devnull)
    except OSError:
        # No usable stderr to swap out. Import anyway and accept the noise;
        # a missing descriptor is not a reason to fail to load the runtime.
        if saved is not None:
            os.close(saved)
            saved = None

    try:
        import onnxruntime
    finally:
        if saved is not None:
            try:
                os.dup2(saved, 2)
            finally:
                os.close(saved)
    return onnxruntime


# ── curl_cffi transport chatter ──────────────────────────────────────────────
#
# curl_cffi's HTTP/2 layer prints this on hosts that close a connection without
# a TLS close_notify:
#
#     h2 connection driver error: peer closed connection without sending TLS
#     close_notify: https://docs.rs/rustls/...
#
# It is written from Rust straight to fd 2, and it is not an error we have any
# say in: an unclean shutdown is the SERVER's choice, the response has already
# been received in full by the time it appears, and the fetch succeeds. Left
# alone it prints on every affected fetch and buries anything worth reading.
#
# Suppressed at the descriptor, around the call only -- not process-wide, which
# would also hide the next real traceback.


class quiet_fd2:
    """Silence writes to file descriptor 2 for the duration of a block.

    A context manager rather than a decorator because the noisy call is one
    line inside a function that has other things to say.

    Degrades to doing nothing if the descriptor cannot be swapped: no usable
    stderr is not a reason to fail a fetch.
    """

    def __enter__(self):
        self._saved = None
        try:
            sys.stderr.flush()
        except Exception:
            pass
        try:
            self._saved = os.dup(2)
            devnull = os.open(os.devnull, os.O_WRONLY)
            try:
                os.dup2(devnull, 2)
            finally:
                os.close(devnull)
        except OSError:
            if self._saved is not None:
                os.close(self._saved)
                self._saved = None
        return self

    def __exit__(self, *exc):
        if self._saved is not None:
            try:
                os.dup2(self._saved, 2)
            finally:
                os.close(self._saved)
                self._saved = None
        return False
