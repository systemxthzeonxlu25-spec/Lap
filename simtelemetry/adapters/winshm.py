"""只读打开游戏创建的命名共享内存（Windows file mapping）。

为什么不直接用 mmap.mmap(-1, size, tagname=...)：
Python 的 mmap 在 Windows 上走 CreateFileMapping —— 名字不存在时会“创建”一块全 0 的新映射。
  1) 游戏没开时，我们会读到一堆 0 而不是“没连上”；
  2) 更糟的是，如果我们先创建了这个名字、且大小比游戏需要的小，游戏启动后
     CreateFileMapping 会拿到我们这块（大小以先创建者为准），游戏自己的 MapViewOfFile 可能失败，
     连带其它遥测软件一起读不到数据。
所以这里用 OpenFileMappingW：只打开已存在的映射，不存在就返回 False，安静重试。
映射整个 section（长度 0），再用 VirtualQuery 得到实际可读字节数，拷贝时绝不越界——
即使结构体定义比游戏实际写的大，也只会少读几个字段，不会访问越界崩溃。
"""
from __future__ import annotations

import ctypes
import logging
import sys

log = logging.getLogger(__name__)

IS_WINDOWS = sys.platform == "win32"

FILE_MAP_READ = 0x0004
ERROR_FILE_NOT_FOUND = 2

if IS_WINDOWS:
    from ctypes import wintypes

    _k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _OpenFileMappingW = _k32.OpenFileMappingW
    _OpenFileMappingW.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR)
    _OpenFileMappingW.restype = wintypes.HANDLE

    _MapViewOfFile = _k32.MapViewOfFile
    _MapViewOfFile.argtypes = (wintypes.HANDLE, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                               ctypes.c_size_t)
    _MapViewOfFile.restype = ctypes.c_void_p

    _UnmapViewOfFile = _k32.UnmapViewOfFile
    _UnmapViewOfFile.argtypes = (ctypes.c_void_p,)
    _UnmapViewOfFile.restype = wintypes.BOOL

    _CloseHandle = _k32.CloseHandle
    _CloseHandle.argtypes = (wintypes.HANDLE,)
    _CloseHandle.restype = wintypes.BOOL

    class _MEMORY_BASIC_INFORMATION(ctypes.Structure):
        _fields_ = [
            ("BaseAddress", ctypes.c_void_p),
            ("AllocationBase", ctypes.c_void_p),
            ("AllocationProtect", wintypes.DWORD),
            ("RegionSize", ctypes.c_size_t),
            ("State", wintypes.DWORD),
            ("Protect", wintypes.DWORD),
            ("Type", wintypes.DWORD),
        ]

    _VirtualQuery = _k32.VirtualQuery
    _VirtualQuery.argtypes = (ctypes.c_void_p, ctypes.POINTER(_MEMORY_BASIC_INFORMATION),
                              ctypes.c_size_t)
    _VirtualQuery.restype = ctypes.c_size_t


def mapping_exists(name: str) -> bool:
    if not IS_WINDOWS:
        return False
    h = _OpenFileMappingW(FILE_MAP_READ, False, name)
    if h:
        _CloseHandle(h)
        return True
    return False


class SharedMemoryPage:
    """一个命名共享内存页 + 对应的 ctypes 结构体。"""

    def __init__(self, name: str, struct_type: type[ctypes.Structure]):
        self.name = name
        self.struct_type = struct_type
        self._handle = None
        self._addr: int | None = None
        self._size = 0
        self._warned_short = False

    @property
    def is_open(self) -> bool:
        return self._addr is not None

    @property
    def mapped_size(self) -> int:
        return self._size

    def open(self) -> bool:
        if self.is_open:
            return True
        if not IS_WINDOWS:
            return False
        h = _OpenFileMappingW(FILE_MAP_READ, False, self.name)
        if not h:
            return False                      # 通常是 ERROR_FILE_NOT_FOUND：游戏没开
        addr = _MapViewOfFile(h, FILE_MAP_READ, 0, 0, 0)
        if not addr:
            err = ctypes.get_last_error()
            _CloseHandle(h)
            log.warning("MapViewOfFile(%s) 失败，错误码 %d", self.name, err)
            return False
        mbi = _MEMORY_BASIC_INFORMATION()
        if not _VirtualQuery(addr, ctypes.byref(mbi), ctypes.sizeof(mbi)):
            _UnmapViewOfFile(addr)
            _CloseHandle(h)
            return False
        self._handle, self._addr, self._size = h, addr, int(mbi.RegionSize)
        need = ctypes.sizeof(self.struct_type)
        if self._size < need and not self._warned_short:
            self._warned_short = True
            log.warning("%s 实际大小 %d 字节 < 结构体 %d 字节：游戏版本与结构体定义可能不一致，"
                        "超出部分的字段会读成 0", self.name, self._size, need)
        return True

    def read(self):
        """拷贝一份结构体快照（游戏在另一个进程里持续写，所以总是拷贝而不是直接引用）。"""
        if self._addr is None:
            raise RuntimeError(f"{self.name} 未打开")
        obj = self.struct_type()
        n = min(ctypes.sizeof(obj), self._size)
        ctypes.memmove(ctypes.addressof(obj), self._addr, n)
        return obj

    def read_at(self, struct_type: type[ctypes.Structure], offset: int):
        """只拷贝映射里 offset 处的一个结构体（LMU 的映射有 300 多 KB，每帧整块拷贝没必要）。"""
        if self._addr is None:
            raise RuntimeError(f"{self.name} 未打开")
        obj = struct_type()
        n = max(0, min(ctypes.sizeof(obj), self._size - offset))
        if n:
            ctypes.memmove(ctypes.addressof(obj), self._addr + offset, n)
        return obj

    def close(self) -> None:
        if self._addr is not None:
            _UnmapViewOfFile(self._addr)
            self._addr = None
        if self._handle:
            _CloseHandle(self._handle)
            self._handle = None
        self._size = 0


def read_consistent(page: SharedMemoryPage, tries: int = 3):
    """结构体第一个字段是 packetId：读前后 packetId 不变才认为没有读到“写了一半”的数据。"""
    obj = page.read()
    for _ in range(tries):
        again = page.read()
        if again.packetId == obj.packetId:
            return again
        obj = again
    return obj
