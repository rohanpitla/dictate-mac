"""Windows API declarations shared by the Windows-only modules.

Every function used gets explicit argtypes/restype: ctypes' defaults
assume 32-bit ints, which silently truncates handles and pointers on 64-bit
Windows and crashes later, far from the cause.
"""

import ctypes
import sys
from ctypes import wintypes as w

if sys.platform != "win32":
    raise ImportError("dictate_win.winapi is Windows-only")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)

LRESULT = ctypes.c_ssize_t
ULONG_PTR = ctypes.c_size_t
WNDPROC = ctypes.WINFUNCTYPE(LRESULT, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
HWND_MESSAGE = w.HWND(-3)

WM_DESTROY = 0x0002
WM_CLOSE = 0x0010
WM_QUIT = 0x0012
WM_INPUT = 0x00FF


class WNDCLASSEXW(ctypes.Structure):
    _fields_ = [("cbSize", w.UINT), ("style", w.UINT), ("lpfnWndProc", WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", w.HINSTANCE), ("hIcon", w.HICON),
                ("hCursor", w.HANDLE), ("hbrBackground", w.HBRUSH),
                ("lpszMenuName", w.LPCWSTR), ("lpszClassName", w.LPCWSTR),
                ("hIconSm", w.HICON)]


class RAWINPUTDEVICE(ctypes.Structure):
    _fields_ = [("usUsagePage", w.USHORT), ("usUsage", w.USHORT),
                ("dwFlags", w.DWORD), ("hwndTarget", w.HWND)]


class RAWINPUTHEADER(ctypes.Structure):
    _fields_ = [("dwType", w.DWORD), ("dwSize", w.DWORD),
                ("hDevice", w.HANDLE), ("wParam", w.WPARAM)]


class RAWKEYBOARD(ctypes.Structure):
    _fields_ = [("MakeCode", w.USHORT), ("Flags", w.USHORT),
                ("Reserved", w.USHORT), ("VKey", w.USHORT),
                ("Message", w.UINT), ("ExtraInformation", w.ULONG)]


class RAWINPUT_KEYBOARD(ctypes.Structure):
    _fields_ = [("header", RAWINPUTHEADER), ("keyboard", RAWKEYBOARD)]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", w.WORD), ("wScan", w.WORD), ("dwFlags", w.DWORD),
                ("time", w.DWORD), ("dwExtraInfo", ULONG_PTR)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", w.LONG), ("dy", w.LONG), ("mouseData", w.DWORD),
                ("dwFlags", w.DWORD), ("time", w.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", w.DWORD), ("wParamL", w.WORD), ("wParamH", w.WORD)]


class _INPUTUNION(ctypes.Union):
    _fields_ = [("mi", MOUSEINPUT), ("ki", KEYBDINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _anonymous_ = ("u",)
    _fields_ = [("type", w.DWORD), ("u", _INPUTUNION)]


INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002


class FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", w.DWORD), ("dwHighDateTime", w.DWORD)]


class CREDENTIALW(ctypes.Structure):
    _fields_ = [("Flags", w.DWORD), ("Type", w.DWORD),
                ("TargetName", w.LPWSTR), ("Comment", w.LPWSTR),
                ("LastWritten", FILETIME), ("CredentialBlobSize", w.DWORD),
                ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
                ("Persist", w.DWORD), ("AttributeCount", w.DWORD),
                ("Attributes", ctypes.c_void_p), ("TargetAlias", w.LPWSTR),
                ("UserName", w.LPWSTR)]


PCREDENTIALW = ctypes.POINTER(CREDENTIALW)


def _declare(dll, name, restype, *argtypes):
    fn = getattr(dll, name)
    fn.restype = restype
    fn.argtypes = list(argtypes)
    return fn


# --- windows and messages
RegisterClassExW = _declare(user32, "RegisterClassExW", w.ATOM, ctypes.POINTER(WNDCLASSEXW))
UnregisterClassW = _declare(user32, "UnregisterClassW", w.BOOL, w.LPCWSTR, w.HINSTANCE)
CreateWindowExW = _declare(user32, "CreateWindowExW", w.HWND, w.DWORD, w.LPCWSTR,
                           w.LPCWSTR, w.DWORD, ctypes.c_int, ctypes.c_int,
                           ctypes.c_int, ctypes.c_int, w.HWND, w.HMENU,
                           w.HINSTANCE, w.LPVOID)
DestroyWindow = _declare(user32, "DestroyWindow", w.BOOL, w.HWND)
DefWindowProcW = _declare(user32, "DefWindowProcW", LRESULT, w.HWND, w.UINT,
                          w.WPARAM, w.LPARAM)
GetMessageW = _declare(user32, "GetMessageW", w.BOOL, ctypes.POINTER(w.MSG),
                       w.HWND, w.UINT, w.UINT)
TranslateMessage = _declare(user32, "TranslateMessage", w.BOOL, ctypes.POINTER(w.MSG))
DispatchMessageW = _declare(user32, "DispatchMessageW", LRESULT, ctypes.POINTER(w.MSG))
PostMessageW = _declare(user32, "PostMessageW", w.BOOL, w.HWND, w.UINT,
                        w.WPARAM, w.LPARAM)
PostQuitMessage = _declare(user32, "PostQuitMessage", None, ctypes.c_int)
GetModuleHandleW = _declare(kernel32, "GetModuleHandleW", w.HMODULE, w.LPCWSTR)

# --- raw input
RegisterRawInputDevices = _declare(user32, "RegisterRawInputDevices", w.BOOL,
                                   ctypes.POINTER(RAWINPUTDEVICE), w.UINT, w.UINT)
GetRawInputData = _declare(user32, "GetRawInputData", w.UINT, w.HANDLE, w.UINT,
                           w.LPVOID, ctypes.POINTER(w.UINT), w.UINT)
RID_INPUT = 0x10000003
RIM_TYPEKEYBOARD = 1
RIDEV_INPUTSINK = 0x00000100
RIDEV_REMOVE = 0x00000001
RI_KEY_BREAK = 0x01
RI_KEY_E0 = 0x02

# --- keyboard state and input
SendInput = _declare(user32, "SendInput", w.UINT, w.UINT, ctypes.POINTER(INPUT),
                     ctypes.c_int)
GetAsyncKeyState = _declare(user32, "GetAsyncKeyState", ctypes.c_short, ctypes.c_int)
MapVirtualKeyW = _declare(user32, "MapVirtualKeyW", w.UINT, w.UINT, w.UINT)

# --- foreground window and processes
GetForegroundWindow = _declare(user32, "GetForegroundWindow", w.HWND)
GetWindowTextW = _declare(user32, "GetWindowTextW", ctypes.c_int, w.HWND,
                          w.LPWSTR, ctypes.c_int)
GetWindowThreadProcessId = _declare(user32, "GetWindowThreadProcessId", w.DWORD,
                                    w.HWND, ctypes.POINTER(w.DWORD))
OpenProcess = _declare(kernel32, "OpenProcess", w.HANDLE, w.DWORD, w.BOOL, w.DWORD)
CloseHandle = _declare(kernel32, "CloseHandle", w.BOOL, w.HANDLE)
QueryFullProcessImageNameW = _declare(kernel32, "QueryFullProcessImageNameW",
                                      w.BOOL, w.HANDLE, w.DWORD, w.LPWSTR,
                                      ctypes.POINTER(w.DWORD))
OpenProcessToken = _declare(advapi32, "OpenProcessToken", w.BOOL, w.HANDLE,
                            w.DWORD, ctypes.POINTER(w.HANDLE))
GetTokenInformation = _declare(advapi32, "GetTokenInformation", w.BOOL, w.HANDLE,
                               ctypes.c_int, w.LPVOID, w.DWORD,
                               ctypes.POINTER(w.DWORD))
GetCurrentProcess = _declare(kernel32, "GetCurrentProcess", w.HANDLE)
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
TOKEN_QUERY = 0x0008
TokenElevation = 20

# --- clipboard and global memory
OpenClipboard = _declare(user32, "OpenClipboard", w.BOOL, w.HWND)
CloseClipboard = _declare(user32, "CloseClipboard", w.BOOL)
EmptyClipboard = _declare(user32, "EmptyClipboard", w.BOOL)
EnumClipboardFormats = _declare(user32, "EnumClipboardFormats", w.UINT, w.UINT)
GetClipboardData = _declare(user32, "GetClipboardData", w.HANDLE, w.UINT)
SetClipboardData = _declare(user32, "SetClipboardData", w.HANDLE, w.UINT, w.HANDLE)
IsClipboardFormatAvailable = _declare(user32, "IsClipboardFormatAvailable",
                                      w.BOOL, w.UINT)
RegisterClipboardFormatW = _declare(user32, "RegisterClipboardFormatW", w.UINT,
                                    w.LPCWSTR)
GetClipboardSequenceNumber = _declare(user32, "GetClipboardSequenceNumber", w.DWORD)
GlobalAlloc = _declare(kernel32, "GlobalAlloc", w.HGLOBAL, w.UINT, ctypes.c_size_t)
GlobalFree = _declare(kernel32, "GlobalFree", w.HGLOBAL, w.HGLOBAL)
GlobalLock = _declare(kernel32, "GlobalLock", w.LPVOID, w.HGLOBAL)
GlobalUnlock = _declare(kernel32, "GlobalUnlock", w.BOOL, w.HGLOBAL)
GlobalSize = _declare(kernel32, "GlobalSize", ctypes.c_size_t, w.HGLOBAL)
GMEM_MOVEABLE = 0x0002
CF_UNICODETEXT = 13

# --- credentials
CredWriteW = _declare(advapi32, "CredWriteW", w.BOOL, PCREDENTIALW, w.DWORD)
CredReadW = _declare(advapi32, "CredReadW", w.BOOL, w.LPCWSTR, w.DWORD, w.DWORD,
                     ctypes.POINTER(PCREDENTIALW))
CredDeleteW = _declare(advapi32, "CredDeleteW", w.BOOL, w.LPCWSTR, w.DWORD, w.DWORD)
CredFree = _declare(advapi32, "CredFree", None, w.LPVOID)
CRED_TYPE_GENERIC = 1
CRED_PERSIST_LOCAL_MACHINE = 2
ERROR_NOT_FOUND = 1168

# --- single instance, message boxes
CreateMutexW = _declare(kernel32, "CreateMutexW", w.HANDLE, w.LPVOID, w.BOOL, w.LPCWSTR)
ERROR_ALREADY_EXISTS = 183
MessageBoxW = _declare(user32, "MessageBoxW", ctypes.c_int, w.HWND, w.LPCWSTR,
                       w.LPCWSTR, w.UINT)
AllowSetForegroundWindow = _declare(user32, "AllowSetForegroundWindow", w.BOOL, w.DWORD)
SetForegroundWindow = _declare(user32, "SetForegroundWindow", w.BOOL, w.HWND)


def last_error() -> int:
    return ctypes.get_last_error()
