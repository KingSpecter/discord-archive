"""Stop.cmd support: a named Windows event tied to this app folder and its data folder."""
import ctypes
from ctypes import wintypes
import hashlib
import os
from pathlib import Path
import threading

EVENT_MODIFY_STATE = 0x0002
ERROR_FILE_NOT_FOUND = 2
WAIT_OBJECT_0 = 0
WAIT_TIMEOUT = 258


def event_name(app_dir, data_dir):
    app = os.path.normcase(str(Path(app_dir).resolve()))
    data = os.path.normcase(str(Path(data_dir).resolve()))
    return "Local\\DiscordArchiveStop-" + hashlib.sha256((app + "\n" + data).encode()).hexdigest()


def kernel():
    if os.name != "nt":
        raise RuntimeError("Stop.cmd is for Windows. Use Settings > Quit or Ctrl+C on this system.")
    api = ctypes.WinDLL("kernel32", use_last_error=True)
    api.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR]
    api.CreateEventW.restype = wintypes.HANDLE
    api.OpenEventW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    api.OpenEventW.restype = wintypes.HANDLE
    api.SetEvent.argtypes = [wintypes.HANDLE]
    api.SetEvent.restype = wintypes.BOOL
    api.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    api.WaitForSingleObject.restype = wintypes.DWORD
    api.CloseHandle.argtypes = [wintypes.HANDLE]
    api.CloseHandle.restype = wintypes.BOOL
    return api


def request_stop(app_dir, data_dir):
    """Signal the running app. Returns False when nothing is running."""
    api = kernel()
    handle = api.OpenEventW(EVENT_MODIFY_STATE, False, event_name(app_dir, data_dir))
    if not handle:
        error = ctypes.get_last_error()
        if error == ERROR_FILE_NOT_FOUND:
            return False
        raise ctypes.WinError(error)
    try:
        if not api.SetEvent(handle):
            raise ctypes.WinError(ctypes.get_last_error())
        return True
    finally:
        api.CloseHandle(handle)


class StopSignal:
    def __init__(self, server, app_dir, data_dir):
        self.api = kernel()
        self.handle = self.api.CreateEventW(None, True, False, event_name(app_dir, data_dir))
        if not self.handle:
            raise ctypes.WinError(ctypes.get_last_error())
        self.done = threading.Event()
        self.server = server
        self.thread = threading.Thread(target=self.watch, daemon=True, name="archive-stop-signal")
        self.thread.start()

    def watch(self):
        while not self.done.is_set():
            status = self.api.WaitForSingleObject(self.handle, 200)
            if status == WAIT_OBJECT_0:
                self.server.engine.stop.set()
                self.server.shutdown()
                return
            if status != WAIT_TIMEOUT:
                return

    def close(self):
        self.done.set()
        self.thread.join()
        self.api.CloseHandle(self.handle)
