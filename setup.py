#!/usr/bin/env python3
"""The helpers Project Maya's installer (maya.py) uses, from Strata's setup (MIT): the PC checks (GPUs, CPU,
RAM, the page file), the Python packages, llama.cpp's source at the pinned commit, resumable downloads, the
engine's source hash, the shards check and the console messages.  Importing it runs nothing; ./setup.sh and
START-MAYA.bat start maya.py."""
from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import os
import platform
import re
import shutil
import struct
import subprocess
import sys
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
WIN = os.name == "nt"
LLAMA_CPP_COMMIT = "3cf03257f219afbe7334045ff7c6a06ac68c627d"
LLAMA_CPP_ZIP = f"https://github.com/ggml-org/llama.cpp/archive/{LLAMA_CPP_COMMIT}.zip"

PY_PACKAGES = ["numpy", "jinja2", "regex", "pyyaml", "tqdm", "requests", "cmake", "ninja", "pillow", "psutil", "jsonschema>=4.18,<5"]


# ------------------------------------------------------------------------------------------------ output
# A screen of the setup's own while it runs (Project Maya's tools/setup_tui.py): these functions hand it what they
# would print, ask and run.  Each of its methods returns something false when it did not take it (the screen has
# closed): then it is printed as without it.
UI = None


def say(msg=""):
    if UI is not None and UI.say(msg):
        return
    print(msg, flush=True)


def step(n, title):
    if UI is not None and UI.step(n, title):
        return
    say()
    say(f"=== Step {n}: {title} ===")


def ok(msg):
    if UI is not None and UI.ok(msg):
        return
    say(f"  [ok] {msg}")


def warn(msg):
    if UI is not None and UI.warn(msg):
        return
    say(f"  [!]  {msg}")


STOPPED = "Setup stopped. Fix the item above and run it again - everything already done is kept and skipped."


def fail(msg, hint=None, end=STOPPED):
    if UI is not None:
        UI.fail(msg, hint)             # shown until it is read; the setup ends there (the lines below follow it)
    say(f"\n  [X]  {msg}")
    if hint:
        say(f"       {hint}")
    if end:
        say("\n" + end)
    sys.exit(1)


def progress(msg=None, frac=None):
    """A line that rewrites itself (a download's progress; frac: the share done, when known); None ends it."""
    if UI is not None and UI.progress(msg, frac):
        return
    print("\n" if msg is None else f"\r  {msg}   ", end="", flush=True)


def ask(question, choices, default, yes):
    if yes:
        return default
    if UI is not None:
        answer = UI.ask(question, choices, default)
        if answer is not None:
            return answer
    while True:
        try:
            a = input(f"{question} [{default}]: ").strip()
        except EOFError:
            return default
        if not a:
            return default
        if a.lower() in [c.lower() for c in choices]:
            return next(c for c in choices if c.lower() == a.lower())
        say(f"  please answer one of: {', '.join(choices)}")


def run(cmd, cwd=None, env=None, check=True, quiet=False, echo=True):
    if echo:
        say("  > " + " ".join(str(c) for c in cmd))
    # the setup's screen shows the output as it comes (all of it: a quiet command's too) and returns no stdout
    r = UI.run([str(c) for c in cmd], cwd=cwd, env=env) if UI is not None else None
    if r is None:
        r = subprocess.run([str(c) for c in cmd], cwd=cwd, env=env,
                           stdout=subprocess.PIPE if quiet else None, stderr=subprocess.STDOUT if quiet else None,
                           text=True)
    if check and r.returncode != 0:
        if quiet and r.stdout:
            say(r.stdout[-4000:])
        fail(f"command failed (exit {r.returncode}): {Path(str(cmd[0])).name}")
    return r


def out(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=60).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


_JOBS = []     # (Windows) the job objects of end_with_me: open while this process runs, closed by Windows when it ends


def end_with_me(proc) -> bool:
    """Windows: `proc` - and every process it starts (Maya's server: its engine, its vision encoder) - ends when this
    process ends, however it ends: Task Manager or taskkill /F, the console window closed, a crash.  A job object
    that kills its processes when its last handle - this process's, never closed here - closes.  Elsewhere nothing
    to do (POSIX: the server's --parent-pipe).  False when it could not be set up."""
    if not WIN:
        return False
    from ctypes import wintypes

    class Basic(ctypes.Structure):                          # JOBOBJECT_BASIC_LIMIT_INFORMATION
        _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                    ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                    ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                    ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                    ("SchedulingClass", wintypes.DWORD)]

    class Extended(ctypes.Structure):                       # JOBOBJECT_EXTENDED_LIMIT_INFORMATION
        _fields_ = [("Basic", Basic), ("Io", ctypes.c_uint64 * 6), ("ProcessMemoryLimit", ctypes.c_size_t),
                    ("JobMemoryLimit", ctypes.c_size_t), ("PeakProcessMemoryUsed", ctypes.c_size_t),
                    ("PeakJobMemoryUsed", ctypes.c_size_t)]
    try:
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.CreateJobObjectW.restype = wintypes.HANDLE
        k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
        k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
        k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        k32.CloseHandle.argtypes = [wintypes.HANDLE]
        job =k32.CreateJobObjectW(None, None)
        if not job:
            return False
        info = Extended()
        info.Basic.LimitFlags = 0x2000                      # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not k32.SetInformationJobObject(job, 9, ctypes.byref(info), ctypes.sizeof(info)) or \
                not k32.AssignProcessToJobObject(job, int(proc._handle)):   # (9: JobObjectExtendedLimitInformation)
            k32.CloseHandle(job)
            return False
        _JOBS.append(job)
        return True
    except (AttributeError, OSError, TypeError, ValueError):
        return False


def done(path: Path) -> bool:
    """A step's finish mark: <path>.done exists (written only after the step completed)."""
    return path.with_name(path.name + ".done").exists()


def mark(path: Path, text=""):
    path.with_name(path.name + ".done").write_text(text or time.strftime("%Y-%m-%d %H:%M"), encoding="utf-8")


# ------------------------------------------------------------------------------------------------ the PC
def _memory_status():
    """Windows' GlobalMemoryStatusEx: RAM, and the commit limit (ullTotalPageFile = RAM + page file)."""
    class MS(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong)]
    m = MS()
    m.dwLength = ctypes.sizeof(MS)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(m))
    return m


def ram_gb():
    if WIN:
        return _memory_status().ullTotalPhys / 2**30
    for line in open("/proc/meminfo"):
        if line.startswith("MemTotal"):
            return int(line.split()[1]) * 1024 / 2**30
    return 0.0


def page_file_gb():
    """The page file's current size (GB) on Windows, None elsewhere.  The graphics card's memory needs room there
    too: under Windows' driver model every allocation on the card is also charged to the commit (RAM + page file),
    so with the page file off or tiny the engine cannot use the free VRAM (issue #60)."""
    if not WIN:
        return None
    m = _memory_status()
    return max(0.0, (m.ullTotalPageFile - m.ullTotalPhys) / 2**30)


def cpu_info():
    """(name, avx2, avx512): avx512 means everything Strata's fast AVX-512 kernels use (F, BW, VL, VNNI, VBMI),
    the same test the engine makes (cpu_avx512_ok), not just AVX-512F."""
    name, avx2, avx512 = platform.processor() or "unknown CPU", False, False
    if WIN:
        pf = ctypes.windll.kernel32.IsProcessorFeaturePresent
        avx2 = bool(pf(40)) or _cpuid_avx2()      # PF_AVX2_INSTRUCTIONS_AVAILABLE, else the CPU itself (#159)
        n = out(["powershell", "-NoProfile", "-Command", "(Get-CimInstance Win32_Processor).Name"]).strip()
        name = n or name
        avx512 = bool(pf(41)) and _cpuid_avx512_full()
    else:
        try:
            txt = open("/proc/cpuinfo").read()
            flags = set(re.search(r"^flags\s*:\s*(.*)$", txt, re.M).group(1).split())
            avx2 = "avx2" in flags
            avx512 = {"avx512f", "avx512bw", "avx512vl", "avx512_vnni", "avx512vbmi"} <= flags
            m = re.search(r"^model name\s*:\s*(.*)$", txt, re.M)
            name = m.group(1) if m else name
        except OSError:
            pass
    return name, avx2, avx512


def _cpuid_avx512_full() -> bool:
    """Windows has no feature bit for VNNI / VBMI: ask the CPU (CPUID leaf 7) through a tiny machine-code stub."""
    try:
        code = bytes([0x53, 0x49, 0x89, 0xC8, 0xB8, 0x07, 0x00, 0x00, 0x00, 0x31, 0xC9, 0x0F, 0xA2,   # push rbx; r8=rcx; cpuid(7,0)
                      0x41, 0x89, 0x18, 0x41, 0x89, 0x48, 0x04, 0x5B, 0xC3])                   # [r8]=ebx,[r8+4]=ecx; pop rbx
        k32 = ctypes.windll.kernel32
        k32.VirtualAlloc.restype = ctypes.c_void_p
        buf = k32.VirtualAlloc(None, len(code), 0x3000, 0x40)
        if not buf:
            return False
        ctypes.memmove(buf, code, len(code))
        regs = (ctypes.c_uint32 * 2)()
        ctypes.CFUNCTYPE(None, ctypes.c_void_p)(buf)(ctypes.addressof(regs))
        ebx, ecx = regs[0], regs[1]
        need_ebx = (1 << 16) | (1 << 30) | (1 << 31)                   # F, BW, VL
        need_ecx = (1 << 1) | (1 << 11)                                # VBMI, VNNI
        return (ebx & need_ebx) == need_ebx and (ecx & need_ecx) == need_ecx
    except Exception:
        return False


def _run_stub(code: bytes, *args) -> None:
    """Runs a few bytes of x64 machine code (Windows calling convention: the arguments in rcx, rdx)."""
    k32 = ctypes.windll.kernel32
    k32.VirtualAlloc.restype = ctypes.c_void_p
    k32.VirtualFree.argtypes = (ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint32)
    buf = k32.VirtualAlloc(None, len(code), 0x3000, 0x40)
    if not buf:
        raise OSError("VirtualAlloc failed")
    try:
        ctypes.memmove(buf, code, len(code))
        ctypes.CFUNCTYPE(None, *[ctypes.c_void_p] * len(args))(buf)(*args)
    finally:
        k32.VirtualFree(buf, 0, 0x8000)


def _cpuid_avx2() -> bool:
    """AVX2 asked from the CPU (CPUID leaf 7 EBX bit 5), with the OS saving the YMM registers (OSXSAVE + XCR0):
    Windows' IsProcessorFeaturePresent(PF_AVX2) says no on some PCs whose CPU has it (a Ryzen 9 3950X, #159)."""
    try:
        def cpuid(leaf):
            regs = (ctypes.c_uint32 * 4)()
            _run_stub(bytes([0x53, 0x49, 0x89, 0xC8, 0x89, 0xD0, 0x31, 0xC9, 0x0F, 0xA2,      # push rbx; r8=rcx; eax=edx; ecx=0; cpuid
                             0x41, 0x89, 0x00, 0x41, 0x89, 0x58, 0x04, 0x41, 0x89, 0x48, 0x08,  # [r8]=eax, [r8+4]=ebx, [r8+8]=ecx
                             0x41, 0x89, 0x50, 0x0C, 0x5B, 0xC3]),                             # [r8+12]=edx; pop rbx
                      ctypes.addressof(regs), leaf)
            return list(regs)
        if cpuid(0)[0] < 7:
            return False
        ecx1 = cpuid(1)[2]
        if not (ecx1 >> 27) & 1 or not (ecx1 >> 28) & 1:             # OSXSAVE, AVX
            return False
        xcr0 = (ctypes.c_uint32 * 2)()
        _run_stub(bytes([0x49, 0x89, 0xC8, 0x31, 0xC9, 0x0F, 0x01, 0xD0,                        # r8=rcx; ecx=0; xgetbv
                         0x41, 0x89, 0x00, 0x41, 0x89, 0x50, 0x04, 0xC3]), ctypes.addressof(xcr0))
        if xcr0[0] & 6 != 6:                                           # the OS saves XMM and YMM
            return False
        return bool((cpuid(7)[1] >> 5) & 1)
    except Exception:
        return False


def gpus():
    """Every NVIDIA GPU, numbered as nvidia-smi numbers them (by PCI bus, the order the engine is told to use)."""
    s = out(["nvidia-smi", "--query-gpu=index,name,memory.total,compute_cap,driver_version",
             "--format=csv,noheader,nounits"])
    found = []
    for line in s.strip().splitlines():
        try:
            idx, name, mem, cc, drv = [x.strip() for x in line.split(",")]
            found.append({"index": int(idx), "name": name, "vram_gb": float(mem) / 1024.0, "arch": cc.replace(".", ""),
                          "driver": drv})
        except ValueError:
            continue
    return found


GPU_PICK = None                                         # --gpu N (issue #51); None: the card with the most VRAM
                                                        # prompt buffers too


def gpu_info(pick=None):
    """The GPU Strata runs on: `pick` (nvidia-smi's number) if given, else the one with the most VRAM (ties: the
    lower number).  None when there is no NVIDIA GPU.  The dict also says how many there are ("count")."""
    found = gpus()
    if not found:
        return None
    pick = GPU_PICK if pick is None else pick
    if pick is not None:
        g = next((x for x in found if x["index"] == pick), None)
        if g is None:
            fail(f"there is no GPU {pick}: " + ", ".join(f"{x['index']} = {x['name']}" for x in found))
    else:
        g = max(found, key=lambda x: (round(x["vram_gb"]), -x["index"]))
    return {**g, "count": len(found)}


VS2026_MIN_CUDA = (13, 2)       # NVIDIA's Windows install guide first lists VS 2026's toolset (MSVC 195x) in CUDA 13.2


def nvcc_version(nvcc):
    """(major, minor) from `nvcc --version` ('... release 13.2, V13.2.51'), or None when it cannot be run or read."""
    if not nvcc:
        return None
    m = re.search(r"release (\d+)\.(\d+)", out([str(nvcc), "--version"]))
    return (int(m.group(1)), int(m.group(2))) if m else None


def nvcc_takes_vs2026(nvcc):
    """True when that toolkit takes VS 2026's C++ toolset, so a 2026-only PC is usable with it.  NVIDIA's Windows
    install guide names the supported MSVC per release: MSVC 195x / Visual Studio 2026 appears from CUDA 13.2 on,
    while 13.1, 13.0 and every 12.x stop at 193x / VS 2022 - so the toolkit's own release says it."""
    v = nvcc_version(nvcc)
    return v is not None and v >= VS2026_MIN_CUDA    # unknown toolkit: False, the same requirement as CUDA 12.x


def find_vcvars(cuda=True, nvcc=None):
    """Visual Studio's vcvars64.bat for a build, newest first.
    CUDA (the default) asks for 2019/2022 only ([16,18)): a newer one (2026 = version 18) installed next to them
    must not be picked ("unsupported Microsoft Visual Studio version"), and with only a newer one there is none.
    Given `nvcc` of CUDA 13.2 or newer - the release whose install guide first supports VS 2026's toolset - 2026
    is taken as well, but an installed older VS stays the pick: such a PC often has a CUDA 12.x toolkit next to
    the 13.2+ one, and 12.x takes only the older VS - the older one fits every toolkit the PC has.
    ROCm's clang (the HIP build: cuda=False) takes a newer one too, 2019/2022 first."""
    vswhere = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Microsoft Visual Studio/Installer/vswhere.exe"
    if not vswhere.exists():
        return None
    ranges = [["-version", "[16.0,18.0)"]]
    if not cuda or nvcc_takes_vs2026(nvcc):
        ranges.append(["-version", "[18.0,)"])
    for versions in ranges:
        p = out([str(vswhere), "-latest", "-products", "*", *versions, "-requires",
                 "Microsoft.VisualStudio.Component.VC.Tools.x86.x64", "-property", "installationPath"]).strip()
        v = Path(p) / "VC/Auxiliary/Build/vcvars64.bat" if p else None
        if v and v.exists():
            return v
    return None


# ------------------------------------------------------------------------------------------------ downloads
def download(url, dst: Path, what=None):
    """Resumable HTTP(S) download with a progress line; `file://` and plain paths are copied (tests, mirrors).
    A finished file gets a <name>.done mark, so a later run skips it without asking the server."""
    dst.parent.mkdir(parents=True, exist_ok=True)
    if dst.exists() and done(dst):
        ok(f"{what or dst.name} already downloaded")
        return
    if not url.startswith(("http://", "https://")):
        src = Path(url[7:] if url.startswith("file://") else url)
        if not src.exists():
            fail(f"not found: {src}")
        shutil.copyfile(src, dst)
        mark(dst)
        ok(f"{what or dst.name} copied")
        return
    part = dst.with_name(dst.name + ".part")
    total = 0
    for attempt in range(5):
        try:
            req = urllib.request.Request(url, method="HEAD", headers={"User-Agent": "maya-setup"})
            total = int(urllib.request.urlopen(req, timeout=60).headers.get("Content-Length", 0))
            break
        except OSError as e:
            if attempt == 4:
                fail(f"cannot reach {url.split('/')[2]} ({e})", "check your internet connection and run it again")
            time.sleep(5)
    if dst.exists() and total and dst.stat().st_size == total:    # finished by an older setup (no mark yet)
        mark(dst)
        ok(f"{what or dst.name} already downloaded")
        return
    have = part.stat().st_size if part.exists() else 0
    for attempt in range(30):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "maya-setup", "Range": f"bytes={have}-"})
            with urllib.request.urlopen(req, timeout=60) as r, open(part, "ab" if have else "wb") as f:
                if have and r.status != 206:                     # the server ignored the range: start over
                    f.seek(0)
                    f.truncate()
                    have = 0
                last = 0.0
                while True:
                    b = r.read(8 << 20)
                    if not b:
                        break
                    f.write(b)
                    have += len(b)
                    if time.time() - last > 2:
                        last = time.time()
                        size = f"{have / 1e9:6.2f} / {total / 1e9:.2f} GB ({100 * have / total:.0f}%)" if total \
                            else f"{have / 1e6:7.1f} MB"
                        progress(f"{what or dst.name}: {size}", have / total if total else None)
            progress()
            if not total or have >= total:
                break
        except OSError as e:
            progress()
            warn(f"download interrupted ({e}); retrying in 10 s ...")
            time.sleep(10)
    if total and part.stat().st_size != total:
        fail(f"could not finish downloading {dst.name}: {part.stat().st_size:,} bytes on disk, the server says {total:,}",
             "check your internet connection and run it again (the download resumes where it stopped)")
    part.replace(dst)
    mark(dst)
    ok(f"{what or dst.name} downloaded")


def check_shards(shards):
    """Every shard present and whole, or setup stops naming the file and the numbers.  Whole means as long as
    its own tensor directory says (the header is read, the data is not): a truncated copy (--gguf-dir, a .part
    renamed by hand, a download finished by an older setup) otherwise passes as a model file and the engine
    fails much later, at the first tensor that runs past the end."""
    sys.path.insert(0, str(ROOT / "tools"))
    from gguf_reader import GGUFFile
    for s in shards:
        if not s.exists():
            fail(f"missing {s}")
        try:
            g = GGUFFile(s)
        except (ValueError, struct.error) as e:
            fail(f"{s.name} is not a whole GGUF shard ({e})", "delete it and run setup again")
        need = g.data_start + max((t.offset + (t.expected_bytes() or 0) for t in g.tensors), default=0)
        have = s.stat().st_size
        if have < need:
            fail(f"{s.name} is short: {have:,} of {need:,} bytes ({need - have:,} missing)",
                 "delete it and run setup again (or copy the whole file into --gguf-dir)")


def get_llama_cpp():
    """llama.cpp at the pinned commit (ggml for the build, gguf-py for the tools, mtmd for images), as a zip: no git."""
    llama = ROOT / "third_party" / "llama.cpp"
    if (llama / "ggml" / "CMakeLists.txt").exists() and (llama / "gguf-py").is_dir():
        return llama
    z = ROOT / "third_party" / f"llama.cpp-{LLAMA_CPP_COMMIT[:7]}.zip"
    download(LLAMA_CPP_ZIP, z, "llama.cpp source")
    tmp = ROOT / "third_party" / "_unpack"
    shutil.rmtree(tmp, ignore_errors=True)
    with zipfile.ZipFile(z) as f:
        f.extractall(tmp)
    top = next(tmp.iterdir())
    shutil.rmtree(llama, ignore_errors=True)
    # PR #63: on Windows a rename can fail with PermissionError while an antivirus scanner still holds a file of the
    # fresh unpack; shutil.move falls back to copy-and-delete, and a few retries let the scanner finish.  The target
    # is `llama` itself - moving into its parent would keep the zip's `llama.cpp-<sha>` folder name.
    for attempt in range(5):
        try:
            shutil.move(str(top), str(llama))
            break
        except PermissionError:
            if attempt == 4:
                raise
            shutil.rmtree(llama, ignore_errors=True)   # a partial copy from the failed attempt
            time.sleep(2)
    shutil.rmtree(tmp, ignore_errors=True)
    z.unlink(missing_ok=True)
    z.with_name(z.name + ".done").unlink(missing_ok=True)
    return llama


def pip_install(packages, what):
    """pip install into .venv, skipped when the same list was installed before."""
    stamp = Path(sys.prefix) / ".strata-pip.json"
    have = json.loads(stamp.read_text()) if stamp.exists() else []
    need = [p for p in packages if p not in have]
    if not need:
        ok(f"{what} already installed")
        return
    say(f"  Installing {what} ...")
    run([sys.executable, "-m", "pip", "install", "--quiet", "--disable-pip-version-check", *need])
    stamp.write_text(json.dumps(sorted(set(have) | set(need)), indent=0))
    ok(f"{what} installed")


AMD_NAMES = {"gfx1100": "AMD Radeon RX 7900 series (gfx1100)",
             "gfx1201": "AMD Radeon RX 9070 / AI PRO R9700 (gfx1201)",
             "gfx1030": "AMD Radeon RX 6800 / 6900 / PRO V620 / W6800 (gfx1030)",
             "gfx1150": "AMD Radeon 890M / 880M (Ryzen AI 300, Strix Point, gfx1150)"}   # when sysfs has no product name
# when sysfs has no product name
# Windows without ROCm's hipInfo yet: the architecture from the adapter's PCI device ID (Navi 31, Navi 48, the
# Strix Halo / Gorgon Halo APU, Navi 21), else from its name ("AMD Radeon(TM) 8060S Graphics", 8050S, 8065S, ...)
AMD_DEVICE_ARCHS = {"744c": "gfx1100", "7550": "gfx1201", "7551": "gfx1201", "1586": "gfx1151",
                    "73a1": "gfx1030", "73a3": "gfx1030", "73a5": "gfx1030", "73af": "gfx1030", "73bf": "gfx1030"}
AMD_NAME_ARCHS = ((r"Radeon(?:\s*\(TM\))?\s+80[4-6]\dS", "gfx1151"), (r"RX\s*7900", "gfx1100"),
                  (r"RX\s*9070|R9700", "gfx1201"),
                  (r"RX\s*6[89][05]0(?:\s*XT)?\b|PRO\s*V620|PRO\s*W6800", "gfx1030"))   # (not the 6800M / 6800S: Navi 22 / 23)


def parse_hipinfo(text: str) -> list:
    """ROCm's hipInfo output as amd_gpus() lists GPUs, in HIP's order (its "device#" lines)."""
    found = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("device#"):
            num = re.search(r"\d+", s)
            found.append({"index": int(num.group()) if num else len(found), "name": "", "vram_gb": 0.0, "arch": "",
                          "driver": "windows", "vendor": "amd"})
            continue
        key, _, val = s.partition(":")
        key, val = key.strip(), val.strip()
        if not found or not val:
            continue
        g = found[-1]
        if key == "Name":
            g["name"] = val
        elif key == "gcnArchName":
            g["arch"] = val.split(":")[0]               # gfx1151, or gfx90a:sramecc+:xnack- -> gfx90a
        elif key == "totalGlobalMem":
            m = re.match(r"([\d.]+)\s*([KMGT]?B)?", val, re.I)
            if m:
                scale = {"KB": 2**-20, "MB": 2**-10, "GB": 1, "TB": 2**10}.get((m.group(2) or "").upper(), 2**-30)
                g["vram_gb"] = float(m.group(1)) * scale
        elif key in ("isIntegrated", "integrated"):
            g["integrated"] = val not in ("0", "false", "False")
    for g in found:
        g["name"] = g["name"] or f"AMD Radeon ({g['arch'] or 'unknown'})"
    return [g for g in found if g["arch"]]


def parse_display_adapters(text: str) -> list:
    """Windows' AMD display adapters (the registry's display class as JSON, from windows_amd_adapters), numbered in
    its order - HIP's own with one AMD GPU."""
    try:
        rows = json.loads(text) if text.strip() else []
    except ValueError:
        return []
    found = []
    for r in rows if isinstance(rows, list) else [rows]:
        if not isinstance(r, dict):
            continue
        name = str(r.get("DriverDesc") or "AMD Radeon")
        dev = re.search(r"dev_([0-9a-f]{4})", str(r.get("MatchingDeviceId") or ""), re.I)
        arch = AMD_DEVICE_ARCHS.get(dev.group(1).lower(), "") if dev else ""
        arch = arch or next((a for pat, a in AMD_NAME_ARCHS if re.search(pat, name, re.I)), "unknown")
        try:
            vram = int(r.get("HardwareInformation.qwMemorySize") or 0) / 2**30
        except (TypeError, ValueError):
            vram = 0.0
        found.append({"index": len(found), "name": name, "vram_gb": vram, "arch": arch,
                      "driver": str(r.get("DriverVersion") or "windows"), "vendor": "amd", "source": "registry"})
    return found


def windows_amd_adapters() -> list:
    # the registry's display class has each adapter's full memory size (WMI's AdapterRAM stops at 4 GB)
    ps = ("Get-ItemProperty 'HKLM:\\SYSTEM\\CurrentControlSet\\Control\\Class\\{4d36e968-e325-11ce-bfc1-08002be10318}"
          "\\0*' -ErrorAction SilentlyContinue | Where-Object { $_.MatchingDeviceId -match 'ven_1002' } | "
          "Select-Object DriverDesc, MatchingDeviceId, DriverVersion, 'HardwareInformation.qwMemorySize' | "
          "ConvertTo-Json -Compress")
    return parse_display_adapters(out(["powershell", "-NoProfile", "-Command", ps]))


def amd_gpus(hipinfo=None):
    """AMD GPUs from the kernel's KFD topology (the amdgpu driver; no ROCm needed), numbered as HIP numbers them:
    the GPU nodes in order, the CPU nodes skipped.  Integrated GPUs are listed too (of them only Strix Point, gfx1150, is supported).  On Windows:
    ROCm's hipInfo (`hipinfo`, its path), else Windows' AMD display adapters (windows_amd_adapters)."""
    if WIN:
        return (parse_hipinfo(out([str(hipinfo)])) if hipinfo else []) or windows_amd_adapters()
    base = Path("/sys/class/kfd/kfd/topology/nodes")
    found = []
    if not base.is_dir():
        return found
    for node in sorted((p for p in base.iterdir() if p.name.isdigit()), key=lambda p: int(p.name)):
        try:
            props = {}
            for line in (node / "properties").read_text().splitlines():
                k, _, v = line.partition(" ")
                props[k] = v.strip()
            ver = int(props.get("gfx_target_version") or 0)
            if ver == 0 or int(props.get("simd_count") or 0) == 0:
                continue
        except (OSError, ValueError):
            continue
        arch = f"gfx{ver // 10000}{(ver // 100) % 100:x}{ver % 100:x}"
        dev = Path(f"/sys/class/drm/renderD{props.get('drm_render_minor', '')}/device")
        try:
            vram = int((dev / "mem_info_vram_total").read_text()) / 2 ** 30
        except (OSError, ValueError):
            vram = 0.0
        try:
            name = (dev / "product_name").read_text().strip() or f"AMD Radeon ({arch})"
        except OSError:
            name = f"AMD Radeon ({arch})"
        if name == f"AMD Radeon ({arch})" and arch in AMD_NAMES:
            name = AMD_NAMES[arch]
        found.append({"index": len(found), "name": name, "vram_gb": vram, "arch": arch, "driver": "amdgpu",
                      "vendor": "amd"})
    return found


# ------------------------------------------------------------------------------------------------ the engine
def driver_major(gpu):
    try:
        return int(gpu["driver"].split(".")[0])
    except (ValueError, KeyError):
        return 0


ENGINE_SOURCES = ("CMakeLists.txt", "cmake", "src", "include", "third_party/ggml")


def source_hash(parts) -> str:
    """A fingerprint of the files a compiled engine is built from, kept in engine/BUILD.json: when a `git pull`
    changes them, the engine is compiled again (issue #31)."""
    h = hashlib.sha256(LLAMA_CPP_COMMIT.encode())
    for part in parts:
        base = ROOT / part
        for f in [base] if base.is_file() else sorted(x for x in base.rglob("*") if x.is_file()):
            h.update(f.relative_to(ROOT).as_posix().encode() + b"\0" + f.read_bytes().replace(b"\r\n", b"\n"))
    return h.hexdigest()[:16]


def settings_path() -> Path:
    if WIN:
        return Path(os.environ.get("APPDATA") or Path.home() / "AppData" / "Roaming") / "Strata" / "settings.json"
    return Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config") / "strata" / "settings.json"


def load_settings() -> dict:
    try:
        return json.loads(settings_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def save_settings(s: dict) -> None:
    try:
        settings_path().parent.mkdir(parents=True, exist_ok=True)
        settings_path().write_text(json.dumps(s, indent=1), encoding="utf-8")
    except OSError as e:
        warn(f"could not save {settings_path()} ({e})")


def is_wsl() -> bool:
    return sys.platform.startswith("linux") and "microsoft" in platform.uname().release.lower()


def hardware_key(cfg: dict) -> str:
    """What a calibration is valid for: this GPU, CPU and RAM, and the model with its context and images setting
    (the context's KV cache and the image encoder take VRAM from the expert cache)."""
    sel = cfg.get("gpu")
    gl = [gpu_info(i) or {} for i in sel] if isinstance(sel, list) else [gpu_info(sel) or {}]
    g = {"name": " + ".join(x.get("name", "?") for x in gl), "vram_gb": sum(x.get("vram_gb", 0) for x in gl)}
    a = cfg.get("args", [])
    ctx = a[a.index("--max-context") + 1] if "--max-context" in a else "?"
    return "|".join([g.get("name", "?"), f"{g.get('vram_gb', 0):.0f}GB", cpu_info()[0], f"{ram_gb():.0f}GB",
                     cfg.get("model_name", "?"), ctx, "images" if "--vision" in a else "text"])


def calibrate_config(cfg_path: Path) -> bool:
    """Measure the engine's hardware-dependent settings on this PC (tools/calibrate.py), write them into the run
    config and remember them per PC and model in the settings file, so an update or a reinstall keeps them."""
    sys.path.insert(0, str(ROOT / "tools"))
    import calibrate as CAL
    cfg = json.loads(cfg_path.read_text(encoding="utf-8-sig"))
    say()
    say("  Tuning Maya for this PC: the output speed is measured with a few engine settings (the PCIe share, the")
    say("  draft depth, the CPU threads). It takes about 5-10 minutes; the PC is busy meanwhile.")
    try:
        res = CAL.run(cfg, say=say)
    except Exception as e:                             # never stops an install: the defaults stay
        warn(f"the tuning did not finish ({e}): the default settings stay")
        return False
    cfg["args"] = CAL.apply(cfg["args"], res["settings"])
    cfg_path.write_text(json.dumps(cfg, indent=1), encoding="utf-8")
    st = load_settings()
    st.setdefault("calibration", {})[hardware_key(cfg)] = {"settings": res["settings"], "tok_s": res["report"].get("tok_s"),
                                                           "date": time.strftime("%Y-%m-%d")}
    save_settings(st)
    if res["settings"]:
        ok("tuned for this PC: " + ", ".join(f"{k} {v}" for k, v in res["settings"].items())
           + (f" ({res['report']['tok_s']} tok/s)" if res["report"].get("tok_s") else ""))
    else:
        ok("tuned for this PC: the default settings are already the fastest here"
           + (f" ({res['report']['tok_s']} tok/s)" if res["report"].get("tok_s") else ""))
    return True


def saved_calibration(cfg: dict) -> dict | None:
    """The settings an earlier calibration found for this PC and model, if any."""
    return (load_settings().get("calibration") or {}).get(hardware_key(cfg))


def upgrade_config(cfg_path: Path, cfg: dict) -> dict:
    """Configs written before v0.1.13 read prompts in fixed 2048-token chunks; the engine now picks the chunk
    itself (`--prefill auto`: up to 8192, as the free VRAM allows - about 2x faster on long prompts).  Under WSL,
    KV streaming is dropped: its RAM copy must be pinned, and the driver pins only about 1 GB there."""
    a = cfg.get("args", [])
    changed = False
    ver = engine_version(cfg["exe"]) if "--prefill" in a else (0, 0, 0)
    if "--prefill" in a and a[a.index("--prefill") + 1] == "2048" and ver >= (0, 1, 13):
        a[a.index("--prefill") + 1] = "auto"
        changed = True
        ok("prompt reading: the engine now picks its chunk size (--prefill auto)")
    elif "--prefill" in a and a[a.index("--prefill") + 1] == "auto" and (0, 0, 0) < ver < (0, 1, 13):
        a[a.index("--prefill") + 1] = "2048"           # an older engine kept after a failed update (issue #49)
        changed = True
        warn(f"the installed engine is {'.'.join(map(str, ver))}: prompts are read in 2048-token chunks until it is updated")
    if is_wsl() and "--kv-resident" in a:
        i = a.index("--kv-resident")
        del a[i:i + 2]
        changed = True
        ok("WSL: KV streaming off (the driver pins only about 1 GB of RAM); the KV cache stays in VRAM")
    if changed:
        cfg_path.write_text(json.dumps(cfg, indent=1), encoding="utf-8")
    return cfg


def start(cfg_path: Path, port: int | None, gpu: int | list | None = None, open_browser=True, yes=False,
          layer_split=None) -> int:
    cfg = upgrade_config(cfg_path, json.loads(cfg_path.read_text(encoding="utf-8-sig")))
    missing = [p for p in [cfg["exe"], *[a for a in cfg["args"] if a.endswith(".gguf")]] if not Path(p).exists()]
    if missing:
        fail(f"{cfg_path.name} refers to missing files: {missing[0]}", "run it again with --setup to repair")
    cfg_path.touch()                                     # the most recently used model
    if "--mtp" in cfg["args"][:-1]:
        refresh_draft_vocab(Path(cfg["args"][cfg["args"].index("--mtp") + 1]))
    cmd = [sys.executable, str(ROOT / "serve" / "server.py"), "--engine", "strata", "--config", str(cfg_path),
           "--port", str(port or cfg.get("port", 8080))]
    if cfg.get("backend") == "hip":                    # AMD: one card, numbered as HIP numbers them
        if isinstance(gpu, list):
            fail("several GPUs sharing one model: NVIDIA only for now", "use one AMD card (--gpu N)")
        if gpu is not None:
            cmd += ["--gpu", str(gpu)]
        found = []
        use = gpu if gpu is not None else cfg.get("gpu")
        g = next((x for x in amd_gpus() if x["index"] == (use if use is not None else x["index"])
                  and amd_problem(x) is None), None)
        if g is not None:
            ok(f"GPU: {g['name']} ({g['vram_gb']:.0f} GB, AMD)")
    else:
        found = gpus()
    if cfg.get("backend") == "hip":
        pass
    elif isinstance(gpu, list):                        # --gpus: saved, this model runs on these cards from now on
        check_gpus(gpu, found)
        cfg["gpu"], cfg["gpus_asked"] = gpu, True
        cfg["layer_split"] = layer_split or cfg.get("layer_split") or "auto"
        cfg_path.write_text(json.dumps(cfg, indent=1), encoding="utf-8")
        gpu = None
    elif gpu is not None:                              # --gpu N: this start only, on that card
        check_gpus([gpu], found)
        cmd += ["--gpu", str(gpu)]
    else:
        cfg = offer_together(cfg_path, cfg, yes)
    use = gpu if gpu is not None else cfg.get("gpu")
    if cfg.get("backend") == "hip":
        pass
    elif isinstance(use, list):
        check_gpus(use, found, "(chosen for this model) ")
        byid = {g["index"]: g for g in found}
        cfg = ensure_engine_for([byid[i] for i in use], cfg_path, cfg, yes)
        ok("GPUs: " + " + ".join(gpu_name(byid[i]) for i in use) + f" together (layers split {cfg.get('layer_split') or 'auto'})")
    elif found:
        g = next((x for x in found if x["index"] == use), None) if use is not None else max(
            found, key=lambda x: (round(x["vram_gb"]), -x["index"]))
        if g is not None:
            cfg = ensure_engine_for([g], cfg_path, cfg, yes)
            ok("GPU: " + gpu_name(g))
    if open_browser:
        cmd.append("--open")
    gb = 0.0
    if "--native" in cfg["args"]:
        try:
            gb = Path(cfg["args"][cfg["args"].index("--native") + 1]).stat().st_size / 1e9
        except (OSError, IndexError):
            pass
    say()
    say("  " + "-" * 100)
    say(f"  Starting {cfg.get('model_name', 'the model')}: it loads {f'about {gb:.0f} GB' if gb >= 1 else '34-55 GB'} "
        "into RAM and locks part of it for the GPU.")
    say("  While it does, YOUR PC CAN BE SLOW OR STOP RESPONDING FOR 1-3 MINUTES (longer the first time after a")
    say("  restart). That is normal: please wait and don't close this window - the browser opens when it is ready.")
    say("  Later, closing this window stops the model.")
    say("  " + "-" * 100)
    return subprocess.call(cmd)


# the draft subsets setup copied before (sha256): replaced by the current one, a subset made by hand is kept
OLD_DRAFT_VOCABS = {"369151522226a5edaa5f12cfd1e2ae7db8f4fbdbd222f3dcf327dced9597fb25"}   # to 0.1.26: 27 Han tokens


def refresh_draft_vocab(rt: Path) -> None:
    """The draft layer's token subset (data/draft_vocab.bin) in the MTP folder: copied when missing, and an older
    shipped one is replaced, so an update reaches existing installs (0.1.27 added the CJK scripts, #137)."""
    new, dst = ROOT / "data" / "draft_vocab.bin", rt / "draft_vocab.bin"
    if not new.exists() or not rt.is_dir():
        return
    if dst.exists():
        old = hashlib.sha256(dst.read_bytes()).hexdigest()
        if old not in OLD_DRAFT_VOCABS or old == hashlib.sha256(new.read_bytes()).hexdigest():
            return
        ok("draft layer: the token subset now includes Chinese, Japanese and Korean")
    shutil.copyfile(new, dst)


def ensure_engine_for(cards, cfg_path: Path, cfg: dict, yes: bool) -> dict:
    """The installed engine must have code for every card the model starts on: a card added later (--gpus with an
    older or newer generation, #128) or a new GPU in the PC otherwise stops the start with 'no kernel image'.  Such a
    card gets the engine compiled for all of them, before the start."""
    missing = [g for g in cards if not engine_runs_on(g)]
    if not missing:
        return cfg
    info = ROOT / "engine" / "BUILD.json"
    meta = json.loads(info.read_text())
    say()
    say("  The installed engine has no code for " + ", ".join(f"{g['name']} (sm_{g['arch']})" for g in missing) +
        ": it is compiled for " + ("these cards" if len(cards) > 1 else "it") + " now.")
    main = gpu_info(cards[0]["index"])
    archs = sorted({int(x) for x in meta.get("archs", [])} | {int(g["arch"]) for g in cards})
    vision = meta.get("vision") or ("gpu" if (ROOT / "engine" / VEXE).exists() else "none")
    build_engine({**main, "archs": archs}, vision, yes, get_llama_cpp())
    dirs = json.loads(info.read_text()).get("cuda_dirs") or []
    cfg["lib_dirs"] = dirs + [d for d in cfg.get("lib_dirs") or [] if d not in dirs]
    cfg_path.write_text(json.dumps(cfg, indent=1), encoding="utf-8")
    return cfg


def write_run_script(model, cfg_path, port):
    serve = [sys.executable, str(ROOT / "serve" / "server.py"), "--engine", "strata", "--config", str(cfg_path),
             "--port", str(port), "--open"]
    if WIN:
        script = ROOT / f"run-{model.lower()}.bat"
        script.write_text("@echo off\r\ntitle Strata " + model + "\r\ncd /d \"" + str(ROOT) + "\"\r\n" +
                          " ".join(f'"{x}"' for x in serve) + "\r\npause\r\n", encoding="utf-8")
    else:
        script = ROOT / f"run-{model.lower()}.sh"
        script.write_text("#!/bin/sh\ncd \"" + str(ROOT) + "\"\nexec " + " ".join(f'"{x}"' for x in serve) + "\n",
                          encoding="utf-8")
        script.chmod(0o755)
    return script


# ------------------------------------------------------------------------------------------------ main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--family", choices=list(FAMILIES), help="qwen = Qwen3.8-Flash-Next, swift = Swift 1.5")
    ap.add_argument("--model", choices=list(MODELS))
    ap.add_argument("--context", type=int)
    ap.add_argument("--kv", choices=["int8", "q4_0", "k8v4"],
                    help="KV cache precision above 8K context: int8 (default), q4_0 (half the memory, a little less "
                         "precise) or k8v4 (hybrid: INT8 K + 4-bit V, 816 B/cell)")
    ap.add_argument("--vision", choices=["yes", "no", "none", "gpu", "cpu"],
                    help="let the model read images (yes = the encoder on the GPU)")
    ap.add_argument("--experimental-speed-projection", metavar="on|off|GGUF",
                    help="EXPERIMENTAL, off by default: the control vector in data/experimental-speed-projection "
                         "(or another GGUF) as a projection on layers 4-44; see docs/DETAILS.md")
    ap.add_argument("--port", type=int, help="the server's port (default: the one the install was set up with, 8080 for a new one)")
    ap.add_argument("--gpu", help="one GPU, numbered as nvidia-smi numbers them (default: asked when several can be "
                                  "used; with --setup it is saved, when starting it is for that start only)")
    ap.add_argument("--gpus", help="several GPUs sharing one model, as nvidia-smi numbers them: \"0,2\", or \"all\" "
                                   "(every card that can); the first is the main one. Saved, also when starting "
                                   "(see docs/MULTI_GPU.md)")
    ap.add_argument("--layer-split", help="with --gpus: where each later GPU's layers start (\"18\", \"16,32\"); "
                                          "default auto, placed from each GPU's free VRAM")
    ap.add_argument("--host", help="where the server listens: 127.0.0.1 = this PC only (default), 0.0.0.0 = also other "
                                   "devices on your network (issue #26; set --api-key too)")
    ap.add_argument("--api-key", help="require this key from clients (recommended with --host 0.0.0.0)")
    ap.add_argument("--data-dir", help="where the model files go (~70-120 GB): default Strata-data next to this folder, "
                                       "remembered for every Strata folder on this PC")
    ap.add_argument("--models-dir", help="where the GGUF files go (default: <data folder>/models)")
    ap.add_argument("--gguf-dir", help="use GGUF files you already have (a folder with the two shards)")
    ap.add_argument("--yes", action="store_true", help="accept the recommended answers")
    ap.add_argument("--setup", action="store_true", help="install another model or change settings")
    ap.add_argument("--no-start", action="store_true", help="install only, do not start the model")
    ap.add_argument("--build", action="store_true", help="compile the engine instead of using the ready-made one")
    ap.add_argument("--prebuilt", default=os.environ.get("STRATA_PREBUILT_URL", PREBUILT_URL),
                    help="where the ready-made engine is (a URL folder or a local folder)")
    ap.add_argument("--check", action="store_true", help="only check this PC and exit")
    ap.add_argument("--calibrate", action="store_true",
                    help="tune the engine's settings for this PC (about 5-10 minutes), then start the model")
    ap.add_argument("--low-ram", choices=["auto", "on", "off"], default="auto",
                    help="map the model's experts from its folder instead of copying them into RAM (for a PC with a big "
                         "GPU and little RAM); auto: when the experts would not fit the RAM")
    ap.add_argument("--backend", choices=["cuda", "hip"],
                    help="cuda = NVIDIA (default), hip = AMD RX 7900 XT/XTX on Linux (experimental; chosen by itself "
                         "when the PC has no NVIDIA card Strata can use)")
    ap.add_argument("--skip-build", action="store_true", help=argparse.SUPPRESS)
    a = ap.parse_args()
    if a.gpu is not None:                              # --gpu 0,2 means --gpus 0,2 (a user tried it: issue report)
        if "," in a.gpu:
            a.gpus, a.gpu = a.gpus or a.gpu, None
        elif a.gpu.strip().isdigit():
            a.gpu = int(a.gpu)
        else:
            ap.error(f"--gpu takes a GPU number as nvidia-smi numbers them, e.g. --gpu 1 (or --gpus 0,2), not {a.gpu!r}")
    say("Strata - Qwen3.8-Flash-Next on a normal PC (a GPU + system RAM + CPU)")
    data, elsewhere = data_folder(a.data_dir)          # the model files: in the data folder, found from any copy
    roots = [data, *elsewhere]
    if a.models_dir is None:
        a.models_dir = str(data / "models")

    # ---- 0. already installed: just start it
    have = installed_configs()
    explicit = a.setup or a.model or a.family or a.check or a.no_start
    if not have and not explicit:                      # a new copy of Strata (an update unzipped elsewhere): set it
        prev = previous_config(elsewhere, load_settings())   # up like the last one, from the files already here
        if prev is not None:
            ch = choices_from_config(prev)
            if ch["model"]:
                say(f"  Found your earlier install in {prev.parent} ({prev.stem[len('strata-'):]}): setting up this "
                    "copy the same way - the model files are reused, nothing big is downloaded.")
                a.family, a.model, a.context = ch["family"], ch["model"], a.context or ch["context"]
                a.kv = a.kv or ch["kv"]
                a.vision = a.vision or ch["vision"]
                a.experimental_speed_projection = a.experimental_speed_projection or ch["esp"]
                a.host, a.api_key = a.host or ch["host"], a.api_key or ch["api_key"]
                a.port = a.port or ch["port"]
                if isinstance(ch.get("gpu"), list):     # a layer split: set up across the same cards again
                    a.gpus = a.gpus or ",".join(str(g) for g in ch["gpu"])
                    a.layer_split = a.layer_split or ch.get("layer_split")
                else:
                    a.gpu = a.gpu if a.gpu is not None else ch.get("gpu")
                a.yes = True
    global GPU_PICK
    # starting an installed model: --gpus 0,2 (or all) saves those cards for it and starts on them (it used to start
    # on the first one alone unless given with --setup), --gpu N runs this start on one card; neither: the saved
    # choice, and asked once when the PC has cards that could share the model
    run_gpu = (parse_gpus(a.gpus, gpus()) if a.gpus else None) or a.gpu
    port = a.port or 8080                              # a new install's port (issue #32: --port for an existing one)
    if have and a.calibrate and not (a.setup or a.model or a.family or a.check):
        if not a.build:
            update_installed_engine(a.prebuilt)
        pick_cfg = have[0]
        if len(have) > 1:
            say()
            for i, c in enumerate(have, 1):
                say(f"  {i}) {json.loads(c.read_text(encoding='utf-8-sig')).get('model_name', c.stem)}")
            pick_cfg = have[int(ask("Tune which one?", [str(i) for i in range(1, len(have) + 1)], "1", a.yes)) - 1]
        calibrate_config(pick_cfg)
        return 0 if a.no_start else start(pick_cfg, a.port, run_gpu, yes=a.yes, layer_split=a.layer_split)
    if have and not (a.setup or a.model or a.family or a.check or a.no_start):
        if not a.build:
            update_installed_engine(a.prebuilt)
        if len(have) == 1:
            return start(have[0], a.port, run_gpu, yes=a.yes, layer_split=a.layer_split)
        say()
        for i, c in enumerate(have, 1):
            say(f"  {i}) {json.loads(c.read_text(encoding='utf-8-sig')).get('model_name', c.stem)}")
        say(f"  {len(have) + 1}) install another model / change settings")
        pick = int(ask("Which one?", [str(i) for i in range(1, len(have) + 2)], "1", a.yes))
        if pick <= len(have):
            return start(have[pick - 1], a.port, run_gpu, yes=a.yes, layer_split=a.layer_split)

    # ---- 1. the PC
    step(1, "checking your PC")
    found = gpus()
    amd = [] if WIN else amd_gpus()
    nv_ok = any(gpu_problem(g) is None for g in found)
    amd_ok = [g for g in amd if amd_problem(g) is None]
    hip = a.backend == "hip" or (a.backend is None and not nv_ok and bool(amd_ok))
    if a.backend is None and nv_ok and amd_ok:
        # both kinds of card: asked (a first run on such a PC used to take NVIDIA without mentioning the Radeon)
        say()
        say("  This PC has NVIDIA and AMD cards Strata can use:")
        say("  1) NVIDIA: " + ", ".join(f"{g['name']} ({g['vram_gb']:.0f} GB)" for g in found if gpu_problem(g) is None)
            + "   (recommended)")
        say("  2) AMD: " + ", ".join(f"{g['name']} ({g['vram_gb']:.0f} GB)" for g in amd_ok)
            + "   (experimental: compiled here, one GPU, no images - docs/AMD_HIP.md)")
        hip = ask("Which cards?", ["1", "2"], "1", a.yes or a.check) == "2"
        if a.check and not hip:
            say("  (the AMD card: ./setup.sh --backend hip)")
    if hip:                                            # AMD (experimental): one card, compiled here
        if WIN:
            fail("Strata's AMD backend runs on Linux only", "use an NVIDIA RTX 20 series or newer card on Windows")
        say("  Your AMD GPUs:" if amd else "  No AMD GPU found (the amdgpu driver's KFD topology is empty).")
        for g in amd:
            say(f"    GPU {g['index']}: {g['name']}, {g['vram_gb']:.0f} GB VRAM - " + (amd_problem(g) or "can be used"))
        usable = [g for g in amd if amd_problem(g) is None]
        if not usable:
            fail("no AMD GPU Strata can use", "the Maya AMD backend targets gfx1100/gfx1201 (and the experimental Strix Point gfx1150) on Linux with system ROCm 7")
        if a.gpus:
            fail("several GPUs sharing one model: NVIDIA only for now", "use one AMD card (--gpu N)")
        if a.gpu is not None:
            gpu = next((g for g in usable if g["index"] == a.gpu), None)
            if gpu is None:
                fail(f"AMD GPU {a.gpu} cannot be used", "use one of: " + ", ".join(f"--gpu {g['index']}" for g in usable))
        else:
            gpu = max(usable, key=lambda x: (round(x["vram_gb"]), -x["index"]))
        gpu = {**gpu, "count": len(amd), "archs": [gpu["arch"]]}
        sel, multi, chosen = [gpu["index"]], [], [gpu]
        a.gpu = gpu["index"] if len(amd) > 1 else a.gpu
        ok(f"GPU: {gpu['name']}, {gpu['vram_gb']:.1f} GB VRAM, {gpu['arch']} (AMD, experimental: docs/AMD_HIP.md)")
    else:
        if not found:
            fail("no NVIDIA GPU found (nvidia-smi did not answer)",
                 "install the NVIDIA driver from https://www.nvidia.com/drivers and restart the PC"
                 + ("; an AMD RX 7900 XT/XTX: --backend hip" if amd else ""))
        if len(found) > 1 or gpu_problem(found[0]) is not None:
            gpu_table(found)
        sel = choose_gpus(a, found)                    # asked when two or more cards can share the model
        multi = sel if len(sel) > 1 else []
        a.gpu = sel[0]                                 # the main GPU: the checks and the sizing below are its
        GPU_PICK = a.gpu
        gpu = gpu_info(a.gpu)
        chosen = [gpu_info(i) for i in sel]
        gpu["archs"] = sorted({x["arch"] for x in chosen})  # the engine needs code for every one of them
        if multi:
            ok("GPUs: " + " + ".join(gpu_name(x) for x in chosen) + " together (the model's layers are split across them)")
        ok(f"GPU: {gpu['name']}, {gpu['vram_gb']:.1f} GB VRAM, compute capability {cc(gpu)}, driver {gpu['driver']}")
        if driver_major(gpu) < MIN_DRIVER:
            fail(f"the NVIDIA driver is too old ({gpu['driver']}; {MIN_DRIVER} or newer is needed)",
                 "update it with the NVIDIA App or from https://www.nvidia.com/drivers, restart, and run this again")
    if gpu["vram_gb"] < 11:
        warn("less than 12 GB of VRAM: Strata will run, but most experts stay on the CPU and it will be slow")
    ram = ram_gb()
    cpu, avx2, avx512 = cpu_info()
    need = min(d["ram_gb"] for d in MODELS.values())
    low_ok = low_ram_fits("IQ1_M", ram, gpu["vram_gb"]) and a.low_ram != "off"   # the smallest model, mapped
    if ram < need - 4 and not a.check and not low_ok:
        # every model keeps ALL its experts in RAM (23+ GB); VRAM only holds a copy of the most-used ones, so a
        # bigger GPU does not lower this
        fail(f"RAM: {ram:.0f} GB - the smallest model (the Coder) needs about {need} GB",
             "Strata keeps all of the model's experts in RAM (23-50 GB, whatever the GPU) and the GPU holds a copy "
             "of the most-used ones: it needs 32 GB of RAM or more (48 GB for the full model)")
    ok(f"RAM: {ram:.0f} GB" if ram >= need - 4 else f"RAM: {ram:.0f} GB (less than the {need} GB the smallest model needs)"
       + ("; the GPU's VRAM makes up for it (the low-RAM mode)" if ram < need - 4 and low_ok else ""))
    pf = page_file_gb()
    if pf is not None and pf < 4:
        warn(f"Windows' page file is {pf:.1f} GB: the graphics card's memory needs room there too (issue #60), so "
             "the model may not start or may use less VRAM. Set it to \"System managed\": System > About > "
             "Advanced system settings > Performance > Advanced > Virtual memory")
    ok(f"CPU: {cpu} ({'AVX-512' if avx512 else 'AVX2' if avx2 else 'no AVX2'})")
    if not avx2:
        fail("this CPU has no AVX2; Strata needs at least AVX2")
    if a.check:
        say()
        for m, d in MODELS.items():
            verdict = "fits" if ram >= d["ram_gb"] else "tight" if ram >= d["ram_gb"] - 8 else "does not fit"
            if low_ram_needed(m, ram) and low_ram_fits(m, ram, gpu["vram_gb"]) and a.low_ram != "off":
                verdict = (f"fits in the low-RAM mode (the GPU holds ~{100 * low_ram_gpu_share(m, gpu['vram_gb']):.0f}% "
                           "of its experts)")
            say(f"  {m:8s} needs ~{d['ram_gb']} GB RAM: {verdict}")
        say("\nThis PC can run Strata. Run it again without --check to install.")
        return 0

    # ---- 2. the questions
    step(2, "your choices")
    fams = list(FAMILIES)
    if a.family:
        family = a.family
    else:
        for i, f in enumerate(fams, 1):
            d = FAMILIES[f]
            say(f"  {i}) {d['title']:20s} {d['by']} - {d['about']}")
        family = fams[int(ask("Which model?", [str(i) for i in range(1, len(fams) + 1)], "1", a.yes)) - 1]
    fam = FAMILIES[family]
    ok(f"model: {fam['title']}")
    if fam.get("license"):
        say(f"  Its license: {fam['license']}")
    say()
    names = [m for m in MODELS if family in MODELS[m].get("families", ("qwen", "swift"))]
    if a.model and a.model not in names:
        fail(f"{fam['title']} has no {a.model} model file", "choose one of: " + ", ".join(names))
    for i, m in enumerate(names, 1):
        d = MODELS[m]
        fit = "" if ram >= d["ram_gb"] else f"   <- needs {d['ram_gb']} GB RAM, you have {ram:.0f}"
        if low_ram_needed(m, ram) and low_ram_fits(m, ram, gpu["vram_gb"]) and a.low_ram != "off":
            fit = f"   <- fits in the low-RAM mode (the GPU holds ~{100 * low_ram_gpu_share(m, gpu['vram_gb']):.0f}%)"
        say(f"  {i}) {m:8s} {d['about']}; download {d['download_gb']:.0f} GB, uses ~{d['arena_gb']:.0f} GB of RAM{fit}")
    rec = str(names.index("IQ3_XXS") + 1) if ram >= 60 and "IQ3_XXS" in names else "1"
    model = a.model or names[int(ask("Which size?", [str(i) for i in range(1, len(names) + 1)], rec, a.yes)) - 1]
    low_ram = (a.low_ram == "on" or (a.low_ram == "auto" and low_ram_needed(model, ram))) and family != "glm"
    if family == "glm":
        ok("GLM-5.3-Flash streams its experts from the model folder by design (the device expert pool); "
           "no separate low-RAM mode")
    if low_ram and multi:
        warn("the low-RAM mode runs on one GPU: using " + gpu_name(gpu) + " only")
        multi, sel, chosen = [], [gpu["index"]], [gpu]
    if low_ram:
        share = low_ram_gpu_share(model, gpu["vram_gb"])
        ok(f"low-RAM mode: {model}'s experts ({MODELS[model]['arena_gb']:.0f} GB) are read from the model folder "
           f"through the OS file cache instead of a copy in RAM ({ram:.0f} GB); the GPU holds ~{100 * share:.0f}% of them")
        if share < 0.6:
            warn("most of the experts are read from the SSD while it answers: expect it to be much slower than with "
                 "enough RAM (a faster SSD and a smaller size help)")
    elif ram < MODELS[model]["ram_gb"] - 4:
        # #125: a warning and a question, not a stop: the user may accept paging (asked, "no" by default, so an
        # unattended --yes install still stops here)
        need_gb, arena = MODELS[model]["ram_gb"], MODELS[model]["arena_gb"]
        warn(f"{model} needs about {need_gb} GB of RAM and this PC has {ram:.0f} GB: its experts alone are "
             f"{arena:.0f} GB and must stay in RAM, so Windows/Linux will page part of them from disk. Expect it "
             "to be much slower, and it may not start at all.")
        say("       A smaller size (Q2_0 or IQ2_XS) fits; more RAM fixes it.")
        if ask("  Install it anyway?", ["y", "n"], "n", a.yes) != "y":
            fail(f"{model} needs about {need_gb} GB of RAM; this PC has {ram:.0f} GB",
                 "choose Q2_0 or IQ2_XS, or add RAM")
        warn(f"installing {model} with {ram:.0f} GB of RAM, as you chose")
    ok(f"size: {model}")
    tag = fam["tag"] + model                           # names of the pack, config and start script
    small = min(x["vram_gb"] for x in chosen)         # each card keeps its layers' KV of the whole context
    rec_ctx = 32768 if small < 14 else 65536 if small < 20 else 131072
    if a.context:
        ctx = a.context
    else:
        say()
        say("  Context length = how much text the model can see at once (your chat, files, tool output).")
        say("  Longer needs more VRAM for it, so fewer experts fit on the GPU:")
        for i, c in enumerate(CONTEXTS, 1):
            say(f"  {i}) {c // 1024}K tokens" + ("   (recommended for your GPU)" if c == rec_ctx else ""))
        ctx = CONTEXTS[int(ask("Context?", [str(i) for i in range(1, 6)], str(CONTEXTS.index(rec_ctx) + 1), a.yes)) - 1]
    if model in ("IQ3_XXS", "IQ3_S") and ram < 90 and ctx > 131072:
        warn(f"{model} with a 262K context needs more than 64 GB of RAM ({MODELS[model]['arena_gb']:.0f} GB of experts "
             "+ the context): using 128K")
        ctx = 131072
    ok(f"context: {ctx} tokens")
    # the KV cache (the model's memory of the conversation): 8-bit, or 4-bit after a Hadamard rotation (PR #21)
    kv = "fp16" if ctx <= 8192 else (a.kv or "int8")
    if ctx > 8192 and not a.kv and not a.yes:
        say()
        say("  KV cache precision (the model's memory of the conversation):")
        say("  1) 8-bit   (recommended: what every published number was measured with)")
        say("  2) 4-bit   half the memory (about 4% faster at 128K), but measurably less precise on long")
        say("             documents; long-context lookups (needle tests) still pass")
        kv = ["int8", "q4_0"][int(ask("KV cache?", ["1", "2"], "1", a.yes)) - 1]
    if ctx > 8192:
        ok(f"KV cache: {'8-bit' if kv == 'int8' else '4-bit (Hadamard-rotated)'}")
    if hip:
        vision = "none"
        if a.vision not in (None, "no", "none"):
            warn("images are not available with the AMD backend yet: off")
    elif family == "glm":
        vision = "none"
        if a.vision not in (None, "no", "none"):
            warn("GLM-5.3-Flash's image tower is not served by the engine yet: images are off")
    elif a.vision:
        vision = {"yes": "gpu", "no": "none"}.get(a.vision, a.vision)
    else:
        say()
        say("  Images: the model can also read pictures (screenshots, photos, scanned pages). This adds a 0.9 GB")
        say("  download and keeps ~1.4 GB of VRAM free for the image encoder, so text is a few % slower.")
        vision = "gpu" if ask("Do you want images?", ["y", "n"], "n", a.yes) == "y" else "none"
    ok("images: " + {"none": "off", "gpu": "on", "cpu": "on (encoder on the CPU)"}[vision])
    # EXPERIMENTAL: the experimental-speed-projection control vector (data/experimental-speed-projection), off unless
    # chosen here; with it loaded, the web app and the API switch it off per request
    esp = None
    esp_choice = (a.experimental_speed_projection or "").strip()
    if family in ("qwen", "coder"):                   # the Coder: the same model's residual stream
        if not esp_choice:
            say()
            say("  EXPERIMENTAL - speed projection: a small control vector applied while the model runs (layers 4-44).")
            say("  It changes how the model answers: its package describes it as a refusal-direction projection (the")
            say("  model declines far fewer requests). Off unless you choose it; when on, the web app can switch it off")
            say("  per chat. Details: data/experimental-speed-projection/README.md")
            esp_choice = "on" if ask("Turn on the experimental speed projection?", ["y", "n"], "n", a.yes) == "y" else "off"
        if esp_choice.lower() not in ("off", "no", "n", "0"):
            esp = ESP_VECTOR if esp_choice.lower() in ("on", "yes", "y", "1") else Path(esp_choice).expanduser().resolve()
            if not esp.is_file():
                fail(f"the experimental speed projection's vector is missing: {esp}")
        ok("experimental speed projection: " + ("ON (experimental)" if esp else "off"))
    elif esp_choice.lower() not in ("", "off", "no", "n", "0"):
        warn("the experimental speed projection is made for the original Qwen3.8-Flash-Next, not Swift 1.5: left off")
    models_dir = Path(a.gguf_dir) if a.gguf_dir else Path(a.models_dir) / tag
    shards = [models_dir / fam["file"].format(q=model, i=i) for i in range(1, fam.get("shards", 2) + 1)]
    if not a.gguf_dir and not all(sh.exists() and done(sh) for sh in shards):
        for r in elsewhere:                            # already downloaded in a Strata folder on another drive
            cand = [r / "models" / tag / sh.name for sh in shards]
            if all(c.exists() and done(c) for c in cand):
                models_dir, shards = cand[0].parent, cand
                ok(f"model files found in {models_dir}")
                break
    have_model = all(s.exists() and (done(s) or a.gguf_dir) for s in shards)
    need = (0 if a.gguf_dir or have_model else MODELS[model]["download_gb"]) + 8 + \
        (40 if model == "Q2_0" and avx512 and family == "qwen" else 0) + (1 if vision != "none" else 0) + \
        (MODELS[model]["arena_gb"] + 1 if low_ram and not (model == "Q2_0" and avx512 and family == "qwen") else 0)
    if free_gb(models_dir) < need:
        fail(f"not enough free disk space in {models_dir}: need ~{need:.0f} GB", "use --models-dir on a bigger drive")

    # ---- 3. python packages
    step(3, "Python packages")
    pip_install(PY_PACKAGES, "numpy, jinja2, regex, pyyaml, tqdm, requests, cmake, ninja, pillow, psutil")

    # ---- 4. the engine
    step(4, "the Strata engine")
    llama = get_llama_cpp()
    ok(f"llama.cpp {LLAMA_CPP_COMMIT[:7]} (gguf-py, ggml, mtmd)")
    eng = None if a.build or hip else get_prebuilt(a.prebuilt, gpu, vision)
    if eng is not None and json.loads((eng / "BUILD.json").read_text()).get("source") != "local":
        pip_install(CUDA_WHEELS, "NVIDIA CUDA libraries (cuBLAS, CUDA runtime; ~0.4 GB)")
        if vision != "none" and not (eng / VEXE).exists():
            warn("the ready-made engine has no image encoder: compiling it")
            eng = None
        elif vision == "gpu":                          # the encoder can cover fewer cards than the engine (RTX 20)
            m = json.loads((eng / "BUILD.json").read_text())
            va = [int(x) for x in m.get("vision_archs", m.get("archs", []))]
            if va and int(gpu["arch"]) not in va and not (m.get("ptx") and int(gpu["arch"]) > max(va)):
                warn(f"the ready-made image encoder has no code for your GPU (sm_{gpu['arch']}): compiling it")
                eng = None
    if eng is None:
        eng = build_engine_hip(gpu, llama) if hip else build_engine(gpu, vision, a.yes, llama)
    meta = json.loads((eng / "BUILD.json").read_text())
    lib_dirs = meta.get("lib_dirs") or meta.get("cuda_dirs") or cuda_lib_dirs()
    ok(f"engine: {eng / EXE}")

    # ---- 5. the model files
    step(5, f"downloading {fam['title']} {model}")
    if not a.gguf_dir:
        for s in shards:
            if s.exists() and done(s):
                ok(f"{s.name} already downloaded")
                continue
            # the original's shard 2 is the same file for all its sizes and the Coder: reuse one that is already here
            other = [p for p in Path(a.models_dir).glob("*/Qwen3.8-Flash-Next-GSQ-RCO-*-00002-of-00002.gguf") if done(p)]
            if family in ("qwen", "coder") and s.name.endswith("00002-of-00002.gguf") and other and not s.exists():
                try:
                    os.link(other[0], s)
                    mark(s)
                    ok(f"{s.name} shared with {other[0].parent.name} (identical file)")
                    continue
                except OSError:
                    pass
            download(fam["hf"].format(q=model) + s.name, s)
    check_shards(shards)
    ok("model files present")
    mmproj = None
    if fam.get("mmproj"):
        mmproj = Path(a.models_dir) / fam["mmproj"]
        if not mmproj.exists():
            mmproj = find_in(roots, f"models/{fam['mmproj']}") or mmproj
    if vision != "none":
        if not mmproj.exists() and a.gguf_dir and (Path(a.gguf_dir) / fam["mmproj"]).exists():
            mmproj = Path(a.gguf_dir) / fam["mmproj"]
        else:
            download(fam["mmproj_hf"] + fam["mmproj"], mmproj, "vision encoder")
        ok(f"vision encoder: {mmproj}")

    # ---- 6. the pack and the MTP draft layer
    step(6, "preparing the model for Strata")
    pack = find_in(roots, f"packs/{tag.lower()}") or data / "packs" / tag.lower()
    env = dict(os.environ, STRATA_GGUF_PY=str(llama / "gguf-py"))
    if model == "Q2_0" and avx512 and family == "qwen":
        # the Q2_0 experts repacked for the AVX-512 kernel (the measured speed): a one-time ~40 GB conversion
        if not (pack / "index.txt").exists() or not (pack / "experts.bin").exists():   # index.txt is written last
            say("  Converting the Q2_0 experts for the AVX-512 kernel (one time, ~40 GB written, 2-5 min) ...")
            run([sys.executable, str(ROOT / "tools" / "strata_pack.py"), "build", "--gguf", str(shards[0]),
                 "--out", str(pack), "--skip-hash"], env=env)
            run([sys.executable, str(ROOT / "tools" / "pack_index.py"), "--pack", str(pack)], env=env)
        if not (pack / "tokenizer" / "vocab.json").exists():
            run([sys.executable, str(ROOT / "tools" / "strata_tokenizer.py"), "--gguf", str(shards[0]),
                 "--out", str(pack)], env=env)   # writes <pack>/tokenizer/
    elif not (pack / "native_experts.txt").exists() or not (pack / "tokenizer" / "vocab.json").exists():
        # every tensor as the GGUF stores it; the experts are read from the GGUF at start (seconds to build)
        run([sys.executable, str(ROOT / "tools" / "iq_pack.py"), "--gguf", str(shards[0]), "--out", str(pack)], env=env)
    if low_ram and not (pack / "experts.bin").exists():
        say(f"  Writing the experts into one file for the low-RAM mode (one time, {MODELS[model]['arena_gb']:.0f} GB) ...")
        run([sys.executable, str(ROOT / "tools" / "iq_pack.py"), "--gguf", str(shards[0]), "--out", str(pack),
             "--experts-bin"], env=env)
    ok(f"model prepared: {pack}")
    rt = None
    if family != "glm":   # the MTP draft layer is the qwen checkpoint's; GLM's NextN is not served yet
        mtp = (find_in(roots, "mtp/rt/experts.bin") or data / "mtp/rt/experts.bin").parent.parent
        rt = mtp / "rt"
        if not (rt / "experts.bin").exists():
            say("  The MTP draft layer (speculative decoding, ~2x faster output) comes from the original Qwen checkpoint:")
            say("  only its ~5 GB of MTP tensors are downloaded.")
            run([sys.executable, str(ROOT / "tools" / "mtp_fetch.py"), "fetch", "--out", str(mtp)], env=env)
            run([sys.executable, str(ROOT / "tools" / "mtp_pack.py"), "--src", str(mtp), "--experts", "q2_0",
                 "--out", str(mtp / "mtp-q2_0.gguf")], env=env)
            run([sys.executable, str(ROOT / "tools" / "mtp_rt.py"), "--gguf", str(mtp / "mtp-q2_0.gguf"), "--out", str(rt)],
                env=env)
        refresh_draft_vocab(rt)
        ok(f"MTP draft layer: {rt}")

    # ---- 7. the start script
    step(7, "writing the start script")
    sys.path.insert(0, str(ROOT / "tools"))
    from gguf_reader import GGUFFile                   # the PLE table's shard: shard 2 (original) or 1 (Swift)
    if family == "glm":
        # M3.1: the verified Glm5Model serving loop behind the standard wire contract - pack in, tokens out;
        # no PLE, no qwen spec/MTP, no vision (src/program/generate.cpp glm_pack_generate)
        args = ["--glm-pack", str(pack), "--max-context", str(ctx)]
    else:
        ple = next((s for s in shards if any(t.name == "per_layer_token_embd.weight" for t in GGUFFile(s).tensors)), None)
        if ple is None:
            fail("the model has no per_layer_token_embd tensor (is this a Qwen3.8-Flash-Next GGUF?)")
        args = ["--pack", str(pack), "--native", str(shards[0]), "--ple-gguf", str(ple),
                "--expert-profile", str(ROOT / "data" / fam.get("profile", "expert-profile.bin")), "--expert-cache", "auto",
                "--prefill", "auto", "--spec", "4", "--spec-min-p", "0.5", "--mtp", str(rt),
                "--max-context", str(ctx)]
        if ctx > 8192:
            args += ["--kv", kv]
        if low_ram:
            args += ["--mmap-experts"]   # the experts from the pack's experts.bin, not copied into RAM
        # KV streaming: from 64K up the whole KV cache lives in RAM and only the part the attention reads (32K positions
        # per layer) stays in VRAM; the VRAM it frees holds more experts (+6% at 128K, +23% at 262K with Q2_0). It
        # costs ~13.7 KB of RAM per context token with 8-bit KV (1.7 GB at 128K), 7.5 KB with 4-bit, so only when it fits.
        kv_ram_gb = ctx * (13 * (576 if kv == "q4_0" else 1056)) / 1e9   # 12 QSA layers + the draft layer
        # Hybrid K8V4 never streams its KV (mode 0 only, layer.hpp), so it is excluded from the WHOLE streaming
        # decision rather than one threshold at a time - a future tier added to this chain cannot reintroduce the
        # combination the engine refuses (PR review).
        if kv == "k8v4":
            if ctx >= 65536:
                ok("KV streaming off: not supported with --kv k8v4; the KV cache stays in VRAM")
        elif is_wsl() and ctx >= 65536:
            ok("WSL: KV streaming off (the driver pins only about 1 GB of RAM); the KV cache stays in VRAM")
        elif ctx >= 65536 and ram >= MODELS[model]["ram_gb"] + kv_ram_gb + 1:
            args += ["--kv-resident", "32768"]
            ok(f"KV streaming on: the context's KV cache lives in RAM ({kv_ram_gb:.1f} GB), more experts fit in VRAM")
    if vision != "none":
        args += ["--vision", "--vram-reserve-mib", str(VISION[vision]["reserve_mib"])]
    if esp is not None:
        # the package's profile, with llama.cpp's flags (the engine takes the same ones)
        args += ["--control-vector-scaled", f"{esp}:1.0", "--control-vector-layer-range", "4", "44",
                 "--cvec-mode", "project", "--cvec-dir", "per-layer"]
    cfg = {"exe": str(eng / EXE), "args": args, "cwd": str(ROOT), "tokenizer": str(pack / "tokenizer"),
           "model_name": f"{fam['name']}-{model.lower()}", "log": str(ROOT / f"strata-{tag.lower()}.log"),
           "lib_dirs": lib_dirs, "port": port}
    if hip:
        cfg["backend"] = "hip"
        # the dense prompt GEMMs through hipBLASLt with kernels measured on this GPU generation (tools/hip; +40-60%
        # prompt speed on the 7900 XTX); the engine refuses a table made for another hipBLASLt version and falls back
        tables = sorted((ROOT / "tools" / "hip").glob(f"{gpu['arch']}-hipblaslt-*.txt"))
        if tables:
            cfg["env"] = {"STRATA_HIPBLASLT_TUNING": str(tables[-1])}
    if gpu["count"] > 1 or a.gpu is not None:
        cfg["gpu"] = gpu["index"]                      # the engine is told this card (issue #51)
        cfg["gpus_asked"] = True                       # chosen at setup: not asked again at start
    if multi:                                          # a layer split across these cards (the server adds the flag)
        cfg["gpu"] = multi
        cfg["layer_split"] = a.layer_split or "auto"
        ok(f"layer split across GPUs {multi} ({cfg['layer_split']})")
    if a.host:
        cfg["host"] = a.host
    if a.api_key:
        cfg["api_key"] = a.api_key
    if vision != "none":
        cfg["vision"] = {"exe": str(eng / VEXE), "mmproj": str(mmproj), "model": str(shards[0]),
                         "gpu": vision == "gpu", "max_tokens": VISION[vision]["max_tokens"]}
        if vision == "cpu":
            cfg["vision"]["threads"] = max(1, (os.cpu_count() or 8) // 2)
    cfg_path = ROOT / f"strata-{tag.lower()}.json"
    cal = None if hip else saved_calibration(cfg)     # tools/calibrate.py is NVIDIA-only for now
    if cal is not None:
        sys.path.insert(0, str(ROOT / "tools"))
        import calibrate as CAL
        cfg["args"] = CAL.apply(cfg["args"], cal.get("settings") or {})
        ok("the settings tuned for this PC earlier are used" + (f" ({cal['date']})" if cal.get("date") else ""))
    cfg_path.write_text(json.dumps(cfg, indent=1), encoding="utf-8")
    script = write_run_script(tag, cfg_path, port)
    # offered only when someone answers: --yes installs and adopted earlier installs are not held up by it
    if cal is None and not hip and not a.no_start and not a.yes and ask(
            "Tune Strata for this PC now? It measures a few engine settings (about 5-10 minutes; the PC is busy "
            "meanwhile; later: START-HERE --calibrate)", ["y", "n"], "y", a.yes) == "y":
        calibrate_config(cfg_path)
    ok(f"start script: {script.name}")

    say()
    say("All set.")
    say(f"  API (OpenAI):     http://127.0.0.1:{port}/v1   (any API key; model name: anything)")
    say(f"  API (Anthropic):  http://127.0.0.1:{port}/v1/messages")
    if a.host and a.host not in ("127.0.0.1", "localhost"):
        say(f"  Other devices:    the server window prints this PC's address (http://<IP>:{port}/)"
            + ("" if a.api_key else " - no API key set: anyone on your network can use it"))
    say(f"  Next time:        just run {'START-HERE.bat' if WIN else './setup.sh'} (or {script.name}) - it starts right away")
    if vision != "none":
        say("  Images:           send them in the chat page, in chat.py (/image <path>) or over the API")
    if a.no_start:
        return 0
    return start(cfg_path, port)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        say("\nstopped.")
        sys.exit(1)
