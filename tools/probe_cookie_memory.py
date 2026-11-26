# -*- coding: utf-8 -*-
"""只读探针：验证能否从 iexplore.exe 进程内存里读到网银会话 Cookie。

不修改任何进程、不注入 DLL，只用 OpenProcess + VirtualQueryEx + ReadProcessMemory
扫描可读内存中的 "JSESSIONID=" 等字符串。纯验证用途。
"""
import ctypes
from ctypes import wintypes

k32 = ctypes.windll.kernel32
psapi = ctypes.windll.psapi


class MBI(ctypes.Structure):
    _fields_ = [
        ("BaseAddress", ctypes.c_void_p),
        ("AllocationBase", ctypes.c_void_p),
        ("AllocationProtect", wintypes.DWORD),
        ("RegionSize", ctypes.c_size_t),
        ("State", wintypes.DWORD),
        ("Protect", wintypes.DWORD),
        ("Type", wintypes.DWORD),
    ]


MEM_COMMIT = 0x1000
READABLE = {0x02, 0x04, 0x20, 0x40, 0x80, 0x01, 0x08}  # 各种 READ/EXECUTE_READ 组合
PROCESS_ACCESS = 0x0010 | 0x0400  # VM_READ | QUERY_INFORMATION

PATTERNS = [b"JSESSIONID=", "JSESSIONID=".encode("utf-16le"),
            b"GLOBALSESSIONID=", "GLOBALSESSIONID=".encode("utf-16le"),
            b"Cookie_ebank=", b"_saberData2="]


def find_iexplore():
    pids = (wintypes.DWORD * 4096)()
    needed = wintypes.DWORD()
    psapi.EnumProcesses(ctypes.byref(pids), ctypes.sizeof(pids), ctypes.byref(needed))
    out = []
    for i in range(needed.value // ctypes.sizeof(wintypes.DWORD)):
        pid = pids[i]
        if not pid:
            continue
        h = k32.OpenProcess(PROCESS_ACCESS, False, pid)
        if not h:
            continue
        buf = ctypes.create_unicode_buffer(260)
        length = wintypes.DWORD(260)
        k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(length))
        name = buf.value.rsplit("\\", 1)[-1].lower()
        if name in ("iexplore.exe", "msedge.exe"):
            out.append((pid, name, h))
        else:
            k32.CloseHandle(h)
    return out


def scan(h, label):
    hits = {p: 0 for p in PATTERNS}
    samples = []
    addr = 0
    mbi = MBI()
    while addr < 0x7FFFFFFFFFFF:
        ok = k32.VirtualQueryEx(h, ctypes.c_void_p(addr), ctypes.byref(mbi), ctypes.sizeof(mbi))
        if not ok:
            break
        size = mbi.RegionSize
        if size == 0:
            break
        if mbi.State == MEM_COMMIT and mbi.Protect in READABLE:
            base = mbi.BaseAddress or 0
            off = 0
            CHUNK = 1024 * 1024
            while off < size:
                n = min(CHUNK, size - off)
                buf = ctypes.create_string_buffer(n)
                got = ctypes.c_size_t()
                if k32.ReadProcessMemory(h, ctypes.c_void_p(base + off), buf, n, ctypes.byref(got)):
                    data = buf.raw[:got.value]
                    for p in PATTERNS:
                        idx = data.find(p)
                        if idx >= 0:
                            hits[p] += 1
                            if len(samples) < 8:
                                end = data.find(b"\x00", idx, idx + 200)
                                if end < 0:
                                    end = idx + 120
                                seg = data[idx:end]
                                samples.append((p.decode("ascii", "ignore"), seg[:160]))
                off += n
        addr = (mbi.BaseAddress or 0) + size
    print(f"[{label}] 命中统计:")
    for p, c in hits.items():
        print(f"   {p.decode('ascii','ignore'):20} -> {c}")
    for name, seg in samples:
        print(f"   样例 [{name}]: {seg!r}")


def main():
    procs = find_iexplore()
    print("找到浏览器进程:", [(p, n) for p, n, _ in procs])
    for pid, name, h in procs:
        try:
            scan(h, f"pid={pid} {name}")
        except Exception as e:
            print(f"[{pid}] 扫描异常: {e}")
        finally:
            k32.CloseHandle(h)


if __name__ == "__main__":
    main()
