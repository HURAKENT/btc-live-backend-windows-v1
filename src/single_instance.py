from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from typing import Self


_ERROR_ALREADY_EXISTS = 183


class AlreadyRunningError(RuntimeError):
    pass


class WindowsMutex:
    def __init__(
        self,
        *,
        handle: wintypes.HANDLE,
        close_handle,
    ) -> None:
        self._handle = handle
        self._close_handle = close_handle
        self._closed = False

    @classmethod
    def acquire(cls, name: str) -> Self:
        if type(name) is not str:
            raise ValueError("INVALID_MUTEX_NAME_TYPE")
        if not name or "\x00" in name:
            raise ValueError("INVALID_MUTEX_NAME")
        if os.name != "nt":
            raise OSError("WINDOWS_NAMED_MUTEX_UNAVAILABLE")

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        create_mutex = kernel32.CreateMutexW
        create_mutex.argtypes = (
            wintypes.LPVOID,
            wintypes.BOOL,
            wintypes.LPCWSTR,
        )
        create_mutex.restype = wintypes.HANDLE
        close_handle = kernel32.CloseHandle
        close_handle.argtypes = (wintypes.HANDLE,)
        close_handle.restype = wintypes.BOOL

        ctypes.set_last_error(0)
        handle = create_mutex(None, False, f"Local\\{name}")
        error_code = ctypes.get_last_error()
        if not handle:
            raise ctypes.WinError(error_code)
        if error_code == _ERROR_ALREADY_EXISTS:
            close_handle(handle)
            raise AlreadyRunningError("BACKEND_ALREADY_RUNNING")
        return cls(handle=handle, close_handle=close_handle)

    def close(self) -> None:
        if self._closed:
            return
        if not self._close_handle(self._handle):
            error_code = ctypes.get_last_error()
            raise ctypes.WinError(error_code)
        self._closed = True

    def __enter__(self) -> Self:
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self.close()
