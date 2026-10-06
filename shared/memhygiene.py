"""
shared/memhygiene.py — shared memory-hygiene helpers. See MEMORY_HYGIENE.md.

Every locally-run script imports `finalize` and calls it at the very end of its
__main__ block (or registers `install` near the start). This guarantees the
process: runs gc, drops large objects, reports PEAK resident memory, flushes
stdout/stderr, and exits cleanly so Windows reclaims the process promptly.

Targets (hard requirement on this RAM-constrained machine):
    utility scripts        < 500 MB peak RSS
    full-history / replay  < 2 GB  peak RSS
A script that exceeds its target is a bug to rework, not ship.

Usage (new scripts):
    from shared.memhygiene import finalize
    if __name__ == "__main__":
        ... work, freeing big objects between stages with del + gc.collect() ...
        finalize("my_script")        # gc + peak-RSS report + sys.exit(0)

Usage (minimal retrofit of an existing script):
    from shared.memhygiene import install
    if __name__ == "__main__":
        install("my_script")         # atexit: gc + peak-RSS report on any exit
        ... work ...
"""
from __future__ import annotations
import atexit
import gc
import sys

_REPORTED = False


def peak_rss_mb():
    """Best-effort PEAK resident-set size in MB (psutil -> POSIX getrusage ->
    Windows ctypes -> None). Peak, not current, so it reflects the worst
    moment of the run."""
    try:
        import psutil
        mi = psutil.Process().memory_info()
        return getattr(mi, "peak_wset", mi.rss) / 1e6     # peak_wset on Windows
    except Exception:
        pass
    try:
        # POSIX stdlib path — the service image has no psutil, and without
        # this the worker (Linux container) could not report its memory.
        import resource
        peak = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        return peak / 1e6 if sys.platform == "darwin" else peak / 1e3   # bytes vs KB
    except Exception:
        pass
    try:
        import ctypes
        k32 = ctypes.WinDLL("kernel32"); psapi = ctypes.WinDLL("psapi")

        class _PMC(ctypes.Structure):
            _fields_ = [("cb", ctypes.c_ulong), ("pf", ctypes.c_ulong),
                        ("peak_ws", ctypes.c_size_t), ("ws", ctypes.c_size_t)] + \
                       [(c, ctypes.c_size_t) for c in "abcdef"]
        k32.GetCurrentProcess.restype = ctypes.c_void_p
        psapi.GetProcessMemoryInfo.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PMC), ctypes.c_ulong]
        c = _PMC(); c.cb = ctypes.sizeof(_PMC)
        psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(c), c.cb)
        return c.peak_ws / 1e6
    except Exception:
        return None


def report_peak_rss(label="script"):
    """Print the peak RSS once (idempotent — safe to call from both an explicit
    finalize() and an atexit handler)."""
    global _REPORTED
    if _REPORTED:
        return
    _REPORTED = True
    rss = peak_rss_mb()
    if rss is not None:
        print(f"[mem] {label} peak RSS = {rss:.0f} MB", flush=True)
    else:
        print(f"[mem] {label} peak RSS = (unavailable)", flush=True)


def finalize(label="script", code=0, *objs):
    """End-of-main teardown: drop any passed big objects, gc, report peak RSS,
    flush, and sys.exit(code) so the process releases immediately."""
    for o in objs:
        try:
            del o
        except Exception:
            pass
    gc.collect()
    report_peak_rss(label)
    sys.stdout.flush()
    sys.stderr.flush()
    sys.exit(code)


def install(label="script"):
    """Register an atexit teardown (gc + peak-RSS report + flush) that fires on
    ANY exit path. Minimal one-line retrofit for existing scripts."""
    def _teardown():
        gc.collect()
        report_peak_rss(label)
        try:
            sys.stdout.flush(); sys.stderr.flush()
        except Exception:
            pass
    atexit.register(_teardown)


__all__ = ["peak_rss_mb", "report_peak_rss", "finalize", "install"]
