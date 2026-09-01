"""_quiet.py: suppressing noise we have checked, without swallowing the rest.

Both messages this module hides are written to file descriptor 2 by C or Rust
code that never passes through `sys.stderr`, which is why no logging filter and
no `contextlib.redirect_stderr` can touch them.

That mechanism is blunt, so the tests here are mostly about its edges: the
descriptor must always be restored, real errors after the block must still be
visible, and a failure to swap descriptors must not take a fetch down with it.
"""
import os
import subprocess
import sys

from dethrottled import _quiet

# ── the descriptor is always put back ────────────────────────────────────────

def test_stderr_is_restored_after_the_block():
    before = os.dup(2)
    try:
        with _quiet.quiet_fd2():
            pass
        after = os.dup(2)
        try:
            assert os.fstat(after).st_dev == os.fstat(before).st_dev
        finally:
            os.close(after)
    finally:
        os.close(before)


def test_stderr_is_restored_even_when_the_block_raises():
    """A suppressed stderr that leaks past an exception would silence the whole
    process for everything that came after it."""
    try:
        with _quiet.quiet_fd2():
            raise ValueError("boom")
    except ValueError:
        pass
    result = subprocess.run(
        [sys.executable, "-c", "import sys; sys.stderr.write('visible')"],
        capture_output=True)
    assert b"visible" in result.stderr


def test_the_context_manager_does_not_swallow_exceptions():
    """It hides output, not errors."""
    raised = False
    try:
        with _quiet.quiet_fd2():
            raise KeyError("must propagate")
    except KeyError:
        raised = True
    assert raised


# ── it actually suppresses, and only inside the block ────────────────────────

SCRIPT = """
import os, sys
sys.path.insert(0, %r)
from dethrottled import _quiet
os.write(2, b"BEFORE ")
with _quiet.quiet_fd2():
    os.write(2, b"HIDDEN ")
os.write(2, b"AFTER ")
"""


def test_only_writes_inside_the_block_are_hidden():
    """Written with os.write so it goes straight to the descriptor, exactly as
    the C and Rust messages this exists for do."""
    import pathlib
    src = str(pathlib.Path(_quiet.__file__).resolve().parents[1])
    result = subprocess.run([sys.executable, "-c", SCRIPT % src],
                            capture_output=True)
    err = result.stderr.decode("utf-8", "replace")
    assert "BEFORE" in err
    assert "AFTER" in err, "stderr must work again afterwards"
    assert "HIDDEN" not in err


# ── degradation ──────────────────────────────────────────────────────────────

def test_a_failure_to_swap_descriptors_is_not_fatal(monkeypatch):
    """No usable stderr is not a reason to fail a fetch. The block must still
    run its body."""
    def refuse(fd):
        raise OSError("no descriptors")

    monkeypatch.setattr(os, "dup", refuse)
    ran = []
    with _quiet.quiet_fd2():
        ran.append(True)
    assert ran == [True]


def test_nested_use_is_safe():
    with _quiet.quiet_fd2():
        with _quiet.quiet_fd2():
            pass
    result = subprocess.run(
        [sys.executable, "-c", "import sys; sys.stderr.write('still here')"],
        capture_output=True)
    assert b"still here" in result.stderr


# ── the onnxruntime loader ───────────────────────────────────────────────────

def test_load_returns_the_module_and_caches_it():
    import importlib.util
    if importlib.util.find_spec("onnxruntime") is None:
        import pytest
        pytest.skip("onnxruntime not installed")
    first = _quiet.load()
    assert first is sys.modules["onnxruntime"]
    assert _quiet.load() is first, "an already-imported module is returned as is"
