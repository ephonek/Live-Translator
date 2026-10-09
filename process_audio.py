"""Windows process-tree WASAPI loopback. No recording files or external binaries.

API reference: Microsoft ApplicationLoopback sample / ActivateAudioInterfaceAsync.
COM interfaces are used on one MTA worker; the completion object is agile.
"""
import ctypes as C
from ctypes import wintypes as W
import os
import sys
import threading
import uuid

P = C.c_void_p
HR = C.c_long
U32 = C.c_uint32
U64 = C.c_uint64
kernel = C.WinDLL('kernel32', use_last_error=True)
ole = C.OleDLL('ole32')
mm = C.WinDLL('Mmdevapi')
kernel.OpenProcess.argtypes = [U32, W.BOOL, U32]
kernel.OpenProcess.restype = P
kernel.CloseHandle.argtypes = [P]
kernel.WaitForSingleObject.argtypes = [P, U32]
kernel.WaitForSingleObject.restype = U32
kernel.GetProcessTimes.argtypes = [P, P, P, P, P]
kernel.CreateEventW.argtypes = [P, W.BOOL, W.BOOL, W.LPCWSTR]
kernel.CreateEventW.restype = P


class GUID(C.Structure):
    _fields_ = [('data', C.c_ubyte * 16)]

    def __init__(self, value):
        super().__init__()
        self.data[:] = uuid.UUID(value).bytes_le


CLIENT = GUID('1cb9ad4c-dbfa-4c32-b178-c2f568a703b2')
CAPTURE = GUID('c8adbd64-e71e-48a0-a4de-185c395cd317')
COMPLETION = GUID('41d949ab-9862-444a-80f6-c261334da5eb')
UNKNOWN = GUID('00000000-0000-0000-c000-000000000046')
AGILE = GUID('94ea2b94-e9cc-49e0-c0ff-ee64ca8f5b90')


def supported():
    return os.name == 'nt' and sys.getwindowsversion().build >= 20348


def check(result, operation):
    if result < 0:
        raise OSError(f'{operation}: HRESULT 0x{result & 0xffffffff:08X}')


def method(ptr, index, result, *types):
    address = C.cast(ptr, C.POINTER(C.POINTER(P))).contents[index]
    return C.WINFUNCTYPE(result, P, *types)(address)


def call(ptr, index, *args, types=()):
    check(method(ptr, index, HR, *types)(ptr, *args), f'Audio method {index}')


def release(ptr):
    if ptr:
        method(ptr, 2, U32)(ptr)


def open_target(pid):
    handle = kernel.OpenProcess(0x1000 | 0x100000, False, pid)
    if not handle:
        raise OSError('無法存取選定程式；它可能已關閉或權限不足。')
    return handle


def creation_time(handle):
    values = [U64() for _ in range(4)]
    if not kernel.GetProcessTimes(handle, *[C.byref(v) for v in values]):
        raise C.WinError(C.get_last_error())
    return values[0].value


def list_applications():
    """Visible windows, grouped by same-executable ancestor (browser root)."""
    class Entry(C.Structure):
        _fields_ = [('size', U32), ('usage', U32), ('pid', U32), ('heap', C.c_size_t),
                    ('module', U32), ('threads', U32), ('parent', U32), ('priority', C.c_long),
                    ('flags', U32), ('exe', W.WCHAR * 260)]
    kernel.CreateToolhelp32Snapshot.argtypes = [U32, U32]
    kernel.CreateToolhelp32Snapshot.restype = P
    kernel.Process32FirstW.argtypes = [P, C.POINTER(Entry)]
    kernel.Process32NextW.argtypes = [P, C.POINTER(Entry)]
    snapshot = kernel.CreateToolhelp32Snapshot(2, 0)
    if snapshot == P(-1).value:
        raise C.WinError(C.get_last_error())
    processes = {}
    try:
        entry = Entry(); entry.size = C.sizeof(entry)
        ok = kernel.Process32FirstW(snapshot, C.byref(entry))
        while ok:
            processes[entry.pid] = (entry.parent, entry.exe)
            ok = kernel.Process32NextW(snapshot, C.byref(entry))
    finally:
        kernel.CloseHandle(snapshot)
    user = C.WinDLL('user32')
    enum_type = C.WINFUNCTYPE(W.BOOL, W.HWND, P)
    user.EnumWindows.argtypes = [enum_type, P]
    user.IsWindowVisible.argtypes = [W.HWND]
    user.GetWindowTextW.argtypes = [W.HWND, W.LPWSTR, C.c_int]
    user.GetWindowThreadProcessId.argtypes = [W.HWND, C.POINTER(U32)]
    found = {}

    @enum_type
    def visit(hwnd, unused):
        if not user.IsWindowVisible(hwnd):
            return True
        title = C.create_unicode_buffer(512)
        user.GetWindowTextW(hwnd, title, len(title))
        pid = U32(); user.GetWindowThreadProcessId(hwnd, C.byref(pid))
        target = pid.value
        if not title.value or target not in processes or target == os.getpid():
            return True
        seen = set()
        while target not in seen:
            seen.add(target)
            parent, name = processes[target]
            if parent not in processes or processes[parent][1].lower() != name.lower():
                break
            target = parent
        if target not in found:
            handle = None
            try:
                handle = open_target(target)
                found[target] = dict(pid=target, created=creation_time(handle),
                                     name=processes[target][1], title=title.value[:80])
            except OSError:
                pass
            finally:
                if handle: kernel.CloseHandle(handle)
        return True
    user.EnumWindows(visit, None)
    return sorted(found.values(), key=lambda row: (row['name'].lower(), row['pid']))


class WaveFormat(C.Structure):
    _pack_ = 2
    _fields_ = [('tag', W.WORD), ('channels', W.WORD), ('rate', U32), ('avg', U32),
                ('align', W.WORD), ('bits', W.WORD), ('extra', W.WORD)]


class Blob(C.Structure):
    _fields_ = [('size', U32), ('data', P)]


class Variant(C.Structure):
    _fields_ = [('vt', W.WORD), ('reserved', W.WORD * 3), ('blob', Blob)]


class Activation(C.Structure):
    _fields_ = [('kind', U32), ('pid', U32), ('mode', U32)]


class ProcessCapture:
    rate, channels = 48000, 2

    def __init__(self, pid, created):
        self.pid, self.created = pid, created
        self.client = P(); self.capture = P(); self.operation = P()
        self.target = self.event = None
        self.initialized = False

    def open(self):
        if not supported():
            raise OSError('指定程式擷取需要 Windows build 20348 以上。')
        check(ole.CoInitializeEx(None, 0), 'CoInitializeEx')
        self.initialized = True
        try:
            self.target = open_target(self.pid)
            if creation_time(self.target) != self.created:
                raise OSError('程式已重新啟動，請重新選擇。')
            done = threading.Event()
            self.activation_error = None
            query_type = C.WINFUNCTYPE(HR, P, P, C.POINTER(P))
            ref_type = C.WINFUNCTYPE(U32, P)
            complete_type = C.WINFUNCTYPE(HR, P, P)

            @query_type
            def query(this, iid, out):
                if C.string_at(iid, 16) in [bytes(g.data) for g in (UNKNOWN, AGILE, COMPLETION)]:
                    out[0] = this
                    return 0
                out[0] = None
                return -2147467262  # E_NOINTERFACE

            @ref_type
            def reference(this): return 2  # Python keeps this object alive until completion.

            @complete_type
            def completed(this, operation):
                try:
                    result = HR()
                    call(operation, 3, C.byref(result), C.byref(self.client),
                         types=(C.POINTER(HR), C.POINTER(P)))
                    check(result.value, 'Activate process loopback')
                except Exception as exc:
                    self.activation_error = exc
                finally:
                    done.set()
                return 0

            self.callbacks = (query, reference, completed)
            self.vtable = (P * 4)(*[C.cast(f, P).value for f in (query, reference, reference, completed)])
            self.com_object = C.pointer(C.cast(self.vtable, P))
            self.activation = Activation(1, self.pid, 0)  # include process tree
            self.variant = Variant(65, (W.WORD * 3)(), Blob(C.sizeof(self.activation), C.addressof(self.activation)))
            mm.ActivateAudioInterfaceAsync.argtypes = [W.LPCWSTR, C.POINTER(GUID), C.POINTER(Variant), P, C.POINTER(P)]
            mm.ActivateAudioInterfaceAsync.restype = HR
            check(mm.ActivateAudioInterfaceAsync('VAD\\Process_Loopback', C.byref(CLIENT),
                  C.byref(self.variant), self.com_object, C.byref(self.operation)), 'ActivateAudioInterfaceAsync')
            # Lifetime must outlast the asynchronous callback. The caller uses a
            # separate worker, so the UI remains responsive during activation.
            done.wait()
            if self.activation_error: raise self.activation_error
            fmt = WaveFormat(1, 2, self.rate, self.rate * 4, 4, 16, 0)
            call(self.client, 3, 0, 0x80060000, 0, 0, C.byref(fmt), None,
                 types=(C.c_int, U32, C.c_int64, C.c_int64, C.POINTER(WaveFormat), P))
            self.event = kernel.CreateEventW(None, False, False, None)
            if not self.event: raise C.WinError(C.get_last_error())
            call(self.client, 13, self.event, types=(P,))
            call(self.client, 14, C.byref(CAPTURE), C.byref(self.capture), types=(C.POINTER(GUID), C.POINTER(P)))
            call(self.client, 10)
            return self
        except Exception:
            self.close()
            raise

    def read(self):
        if kernel.WaitForSingleObject(self.target, 0) == 0:
            raise OSError('選定程式已關閉，請重新選擇音訊來源。')
        kernel.WaitForSingleObject(self.event, 100)
        output = []
        while True:
            size = U32()
            call(self.capture, 5, C.byref(size), types=(C.POINTER(U32),))
            if not size.value: break
            data, frames, flags = P(), U32(), U32()
            call(self.capture, 3, C.byref(data), C.byref(frames), C.byref(flags), None, None,
                 types=(C.POINTER(P), C.POINTER(U32), C.POINTER(U32), P, P))
            try:
                if flags.value & 1 and getattr(self, 'received', False):
                    raise OSError('指定程式音訊中斷，請重新套用來源。')
                output.append(bytes(frames.value * 4) if flags.value & 2 else C.string_at(data, frames.value * 4))
                self.received = True
            finally:
                call(self.capture, 4, frames.value, types=(U32,))
        return b''.join(output)

    def close(self):
        if self.client:
            method(self.client, 11, HR)(self.client)
        for ptr in (self.capture, self.client, self.operation): release(ptr)
        self.capture = P(); self.client = P(); self.operation = P()
        for handle in (self.target, self.event):
            if handle: kernel.CloseHandle(handle)
        self.target = self.event = None
        if self.initialized:
            ole.CoUninitialize()
            self.initialized = False
