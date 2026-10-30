"""Windows job object bounds the worker and its converter process tree."""
import ctypes
import os
from ctypes import wintypes


def bound_worker_memory(limit):
    if os.name != "nt":
        return None
    class Basic(ctypes.Structure):
        _fields_ = [("perProcess", ctypes.c_int64), ("perJob", ctypes.c_int64), ("flags", wintypes.DWORD), ("minimum", ctypes.c_size_t), ("maximum", ctypes.c_size_t), ("active", wintypes.DWORD), ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]
    class IO(ctypes.Structure):
        _fields_ = [(name, ctypes.c_uint64) for name in ("reads", "writes", "other", "readBytes", "writeBytes", "otherBytes")]
    class Extended(ctypes.Structure):
        _fields_ = [("basic", Basic), ("io", IO), ("processMemory", ctypes.c_size_t), ("jobMemory", ctypes.c_size_t), ("peakProcess", ctypes.c_size_t), ("peakJob", ctypes.c_size_t)]
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.GetCurrentProcess.restype = wintypes.HANDLE
    handle = kernel.CreateJobObjectW(None, None)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    info = Extended()
    # JOB_MEMORY + KILL_ON_JOB_CLOSE; the worker holds the handle for its lifetime.
    info.basic.flags, info.jobMemory = 0x200 | 0x2000, limit
    if not kernel.SetInformationJobObject(handle, 9, ctypes.byref(info), ctypes.sizeof(info)) or not kernel.AssignProcessToJobObject(handle, kernel.GetCurrentProcess()):
        raise ctypes.WinError(ctypes.get_last_error())
    return handle
