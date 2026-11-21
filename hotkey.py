from __future__ import annotations

import ctypes
import subprocess
from ctypes import wintypes

STOP_EVENT_NAME = r"Local\ScreenshotSearch.StopHotkey"
HOTKEY_ID = 1
ERROR_HOTKEY_ALREADY_REGISTERED = 1409


def _event_api() -> ctypes.WinDLL:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle = wintypes.HANDLE
    signatures = (
        ("CreateEventW", handle, (wintypes.LPVOID, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR)),
        ("OpenEventW", handle, (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)),
        ("WaitForSingleObject", wintypes.DWORD, (handle, wintypes.DWORD)),
        ("SetEvent", wintypes.BOOL, (handle,)),
        ("CloseHandle", wintypes.BOOL, (handle,)),
    )
    for name, result, arguments in signatures:
        function = getattr(kernel32, name)
        function.restype = result
        function.argtypes = arguments
    return kernel32


def listen_for_hotkey(command: list[str]) -> None:
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    user32.RegisterHotKey.argtypes = (wintypes.HWND, wintypes.INT, wintypes.UINT, wintypes.UINT)
    user32.UnregisterHotKey.argtypes = (wintypes.HWND, wintypes.INT)
    user32.PeekMessageW.argtypes = (
        ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT, wintypes.UINT
    )

    mod_control, mod_no_repeat = 0x0002, 0x4000
    vk_snapshot = 0x2C
    if not user32.RegisterHotKey(None, HOTKEY_ID, mod_control | mod_no_repeat, vk_snapshot):
        error = ctypes.get_last_error()
        if error == ERROR_HOTKEY_ALREADY_REGISTERED:
            raise OSError("Ctrl + Print Screen уже занято другим обработчиком.")
        raise ctypes.WinError(error)

    try:
        kernel32 = _event_api()
        stop_event = kernel32.CreateEventW(None, True, False, STOP_EVENT_NAME)
        if not stop_event:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            print("Ctrl + Print Screen включено. Нажмите Ctrl+C для остановки.", flush=True)
            _process_hotkeys(user32, kernel32, stop_event, command)
        finally:
            kernel32.CloseHandle(stop_event)
    finally:
        user32.UnregisterHotKey(None, HOTKEY_ID)


def _process_hotkeys(
    user32: ctypes.WinDLL, kernel32: ctypes.WinDLL, stop_event: int, command: list[str]
) -> None:
    wm_hotkey, wm_quit, pm_remove = 0x0312, 0x0012, 0x0001
    wait_signaled, wait_timeout = 0, 0x0102
    message = wintypes.MSG()
    capture = None
    while True:
        # The short wait keeps Ctrl+C responsive in console mode.
        result = kernel32.WaitForSingleObject(stop_event, 50)
        if result == wait_signaled:
            return
        if result != wait_timeout:
            raise ctypes.WinError(ctypes.get_last_error())

        while user32.PeekMessageW(ctypes.byref(message), None, wm_hotkey, wm_hotkey, pm_remove):
            if message.message == wm_quit:
                return
            if message.message == wm_hotkey and message.wParam == HOTKEY_ID:
                if capture is None or capture.poll() is not None:
                    capture = subprocess.Popen(command)


def stop_hotkey() -> bool:
    kernel32 = _event_api()
    event_modify_state, error_file_not_found = 0x0002, 2
    event = kernel32.OpenEventW(event_modify_state, False, STOP_EVENT_NAME)
    if not event:
        error = ctypes.get_last_error()
        if error == error_file_not_found:
            return False
        raise ctypes.WinError(error)
    try:
        if not kernel32.SetEvent(event):
            raise ctypes.WinError(ctypes.get_last_error())
        return True
    finally:
        kernel32.CloseHandle(event)
