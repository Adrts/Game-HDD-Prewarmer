r"""Game HDD Prewarmer —— 机械硬盘冷启动预热器（单文件版）。

按磁盘物理扇区顺序把游戏目录里的小文件读入 Windows 页面缓存，
随后让用户尽快启动游戏，减少机械盘随机小 IO 造成的暖机卡顿。

流程：选择游戏目录 → 设置缓存上限与单文件上限 → 扫描 →
      按物理位置排序并截断 → 确认 → 串行读取 → 提示启动游戏。

运行：.\.venv\Scripts\python.exe main.py
"""

from __future__ import annotations

import ctypes
import os
import queue
import threading
import time
import tkinter as tk
from ctypes import wintypes
from dataclasses import dataclass
from tkinter import filedialog, messagebox, ttk
from tkinter.scrolledtext import ScrolledText
from typing import Callable, Iterator, List, Optional, Tuple

GB = 1024 ** 3
MB = 1024 ** 2


# =============================================================================
# 一、Windows 原生调用：查询文件物理起始位置、把文件读入系统页面缓存
# =============================================================================

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

GENERIC_READ = 0x80000000
FILE_SHARE_READ = 0x00000001
FILE_SHARE_WRITE = 0x00000002
FILE_SHARE_DELETE = 0x00000004
OPEN_EXISTING = 3
FSCTL_GET_RETRIEVAL_POINTERS = 0x00090073

_INVALID_HANDLE = ctypes.c_void_p(-1).value

kernel32.CreateFileW.argtypes = (
    wintypes.LPCWSTR,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.LPVOID,
    wintypes.DWORD,
    wintypes.DWORD,
    wintypes.HANDLE,
)
kernel32.CreateFileW.restype = ctypes.c_void_p
kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.DeviceIoControl.argtypes = (
    wintypes.HANDLE,
    wintypes.DWORD,
    wintypes.LPVOID,
    wintypes.DWORD,
    wintypes.LPVOID,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    wintypes.LPVOID,
)
kernel32.DeviceIoControl.restype = wintypes.BOOL
kernel32.ReadFile.argtypes = (
    wintypes.HANDLE,
    wintypes.LPVOID,
    wintypes.DWORD,
    ctypes.POINTER(wintypes.DWORD),
    wintypes.LPVOID,
)
kernel32.ReadFile.restype = wintypes.BOOL


class _RetrievalPointersHeader(ctypes.Structure):
    """RETRIEVAL_POINTERS_BUFFER 的头部，后面紧跟 ExtentCount 个 {NextVcn, Lcn}。"""

    _fields_ = (
        ("ExtentCount", wintypes.DWORD),
        ("StartingVcn", ctypes.c_longlong),
    )


def _open_read(path: str) -> Optional[int]:
    """以只读方式打开文件，共享模式，避免阻塞游戏或启动器。"""
    handle = kernel32.CreateFileW(
        os.fspath(path),
        GENERIC_READ,
        FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE,
        None,
        OPEN_EXISTING,
        0,
        None,
    )
    if not handle or handle == _INVALID_HANDLE:
        return None
    return handle


def first_lcn(path: str) -> Optional[int]:
    """返回文件首个非稀疏区间的 LCN（逻辑簇号），失败返回 None。

    LCN 与磁盘物理位置单调对应，用作物理顺序排序键。
    数据常驻 MFT 的极小文件会返回 ERROR_HANDLE_EOF，此处统一按 None 处理。
    """
    handle = _open_read(path)
    if not handle:
        return None
    try:
        output = ctypes.create_string_buffer(64 * 1024)
        start_vcn = ctypes.c_longlong(0)
        returned = wintypes.DWORD(0)
        ok = kernel32.DeviceIoControl(
            handle,
            FSCTL_GET_RETRIEVAL_POINTERS,
            ctypes.byref(start_vcn),
            ctypes.sizeof(start_vcn),
            output,
            len(output),
            ctypes.byref(returned),
            None,
        )
        if not ok:
            return None
        return _first_extent_lcn(output, returned.value)
    finally:
        kernel32.CloseHandle(handle)


def _first_extent_lcn(buffer, size: int) -> Optional[int]:
    header_size = ctypes.sizeof(_RetrievalPointersHeader)
    if size < header_size:
        return None
    header = _RetrievalPointersHeader.from_buffer(buffer)
    count = header.ExtentCount
    if count <= 0:
        return None
    for index in range(count):
        entry = header_size + index * 16
        if entry + 16 > size:
            break
        lcn = ctypes.c_longlong.from_buffer(buffer, entry + 8).value
        if lcn >= 0:
            return lcn
    return None


def read_all_discard(path: str, buffer) -> int:
    """把整个文件读进 buffer 并丢弃，只为让文件页进入系统页面缓存。

    返回实际读取的字节数；失败抛出 OSError。
    """
    handle = _open_read(path)
    if not handle:
        raise ctypes.WinError(ctypes.get_last_error())
    total = 0
    try:
        while True:
            read = wintypes.DWORD(0)
            if not kernel32.ReadFile(handle, buffer, len(buffer), ctypes.byref(read), None):
                raise ctypes.WinError(ctypes.get_last_error())
            if read.value == 0:
                break
            total += read.value
    finally:
        kernel32.CloseHandle(handle)
    return total


# =============================================================================
# 二、扫描：体积过滤、物理位置采集、排序与预算截断
# =============================================================================


@dataclass
class Candidate:
    path: str
    size: int
    lcn: Optional[int] = None
    file_id: int = 0

    @property
    def order_key(self) -> Tuple[int, int]:
        """有 LCN 的按物理位置排在前；没有的按文件索引（MFT 序号近似）兜底。"""
        if self.lcn is None:
            return (1, self.file_id)
        return (0, self.lcn)


@dataclass
class ScanStats:
    files_seen: int = 0
    matched_files: int = 0
    matched_bytes: int = 0
    skipped_by_size: int = 0
    lcn_missing: int = 0
    elapsed: float = 0.0


def _walk_files(root: str) -> Iterator[Tuple[str, int, int]]:
    """递归产出 (路径, 体积, 文件索引)，跳过符号链接与联接点。"""
    stack = [root]
    visited = set()
    while stack:
        current = stack.pop()
        try:
            real = os.path.realpath(current)
        except OSError:
            real = current
        if real in visited:
            continue
        visited.add(real)
        try:
            entries = os.scandir(current)
        except OSError:
            continue
        with entries:
            for entry in entries:
                try:
                    if entry.is_symlink():
                        continue
                    if entry.is_dir(follow_symlinks=False):
                        is_junction = getattr(entry, "is_junction", None)
                        if is_junction and is_junction():
                            continue
                        stack.append(entry.path)
                    elif entry.is_file(follow_symlinks=False):
                        info = entry.stat(follow_symlinks=False)
                        yield entry.path, info.st_size, info.st_ino
                except OSError:
                    continue


def scan(
    root: str,
    max_file_size: int,
    on_stage: Optional[Callable[[str], None]] = None,
    on_progress: Optional[Callable[[int, int, str], None]] = None,
) -> Tuple[List[Candidate], ScanStats]:
    """收集体积不超过 max_file_size 的文件，并查询各自的物理起始 LCN。"""
    started = time.perf_counter()
    stats = ScanStats()

    if on_stage:
        on_stage("正在遍历目录…")
    candidates: List[Candidate] = []
    for path, size, file_id in _walk_files(root):
        stats.files_seen += 1
        if on_progress and stats.files_seen % 500 == 0:
            on_progress(0, 0, f"已遍历 {stats.files_seen} 个文件，命中 {len(candidates)} 个")
        if size <= 0 or size > max_file_size:
            stats.skipped_by_size += 1
            continue
        candidates.append(Candidate(path=path, size=size, file_id=file_id))

    stats.matched_files = len(candidates)
    stats.matched_bytes = sum(item.size for item in candidates)

    if on_stage:
        on_stage("正在查询文件物理位置…")
    total = len(candidates)
    for index, item in enumerate(candidates, 1):
        item.lcn = first_lcn(item.path)
        if item.lcn is None:
            stats.lcn_missing += 1
        if on_progress and (index % 100 == 0 or index == total):
            on_progress(index, total, f"已定位 {index}/{total} 个文件")

    stats.elapsed = time.perf_counter() - started
    return candidates, stats


def build_plan(
    candidates: List[Candidate], budget_bytes: int
) -> Tuple[List[Candidate], List[Candidate], int, int]:
    """按物理位置升序排序，并在预算内从头部截断。

    返回 (完整排序结果, 入选结果, 入选总体积, 被截断的文件数)。
    """
    ordered = sorted(candidates, key=lambda item: item.order_key)
    selected: List[Candidate] = []
    total = 0
    for item in ordered:
        if total + item.size > budget_bytes:
            break
        selected.append(item)
        total += item.size
    return ordered, selected, total, len(ordered) - len(selected)


# =============================================================================
# 三、预热读取：严格串行，读完即丢弃，只为建立页面缓存
# =============================================================================

DEFAULT_CHUNK_SIZE = 1024 * 1024


@dataclass
class WarmStats:
    files_done: int = 0
    files_failed: int = 0
    bytes_read: int = 0
    elapsed: float = 0.0

    @property
    def avg_mbps(self) -> float:
        if self.elapsed <= 0:
            return 0.0
        return self.bytes_read / self.elapsed / MB


def warm(
    candidates: List[Candidate],
    on_progress: Optional[Callable[[int, int, int, int, str, float], None]] = None,
    chunk_size: int = DEFAULT_CHUNK_SIZE,
    should_stop: Optional[Callable[[], bool]] = None,
) -> WarmStats:
    """严格串行读取候选文件，读完即丢弃，不解析、不保存。

    进度回调参数：(已读字节, 总字节, 已完成文件, 总文件, 当前文件, 已耗时)
    """
    stats = WarmStats()
    total_bytes = sum(item.size for item in candidates)
    total_files = len(candidates)
    buffer = ctypes.create_string_buffer(chunk_size)
    started = time.perf_counter()

    for item in candidates:
        if should_stop and should_stop():
            break
        try:
            stats.bytes_read += read_all_discard(item.path, buffer)
            stats.files_done += 1
        except OSError:
            stats.files_failed += 1
        stats.elapsed = time.perf_counter() - started
        if on_progress:
            on_progress(
                stats.bytes_read,
                total_bytes,
                stats.files_done + stats.files_failed,
                total_files,
                item.path,
                stats.elapsed,
            )

    stats.elapsed = time.perf_counter() - started
    return stats


# =============================================================================
# 四、图形界面
# =============================================================================


def human_bytes(value: float) -> str:
    units = ("B", "KB", "MB", "GB", "TB")
    size = float(value)
    for unit in units:
        if size < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(size)} B"
            return f"{size:.2f} {unit}"
        size /= 1024
    return f"{size:.2f} TB"


def human_duration(seconds: float) -> str:
    total = int(max(0, seconds))
    hours, rest = divmod(total, 3600)
    minutes, secs = divmod(rest, 60)
    if hours:
        return f"{hours:02d}:{minutes:02d}:{secs:02d}"
    return f"{minutes:02d}:{secs:02d}"


class PrewarmerApp(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("Game HDD Prewarmer — 机械硬盘冷启动预热")
        self.geometry("780x600")
        self.minsize(700, 560)
        self.events: "queue.Queue[tuple]" = queue.Queue()
        self.busy = False
        self._build_ui()
        self.after(80, self._poll_events)

    # ---------- 界面 ----------

    def _build_ui(self) -> None:
        style = ttk.Style(self)
        if "vista" in style.theme_names():
            style.theme_use("vista")
        style.configure("Hint.TLabel", foreground="#666666")

        outer = ttk.Frame(self, padding=16)
        outer.pack(fill="both", expand=True)

        ttk.Label(outer, text="Game HDD Prewarmer", font=("Segoe UI", 16, "bold")).pack(anchor="w")
        ttk.Label(
            outer,
            text="把游戏目录里的小文件按磁盘物理顺序读入系统页面缓存，缩短机械硬盘的冷启动暖机时间。",
            style="Hint.TLabel",
        ).pack(anchor="w", pady=(2, 12))

        options = ttk.LabelFrame(outer, text="预热设置", padding=12)
        options.pack(fill="x")
        options.columnconfigure(1, weight=1)

        ttk.Label(options, text="游戏文件夹").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.folder_var = tk.StringVar()
        self.folder_entry = ttk.Entry(options, textvariable=self.folder_var)
        self.folder_entry.grid(row=0, column=1, sticky="ew")
        self.browse_button = ttk.Button(options, text="浏览…", command=self._browse)
        self.browse_button.grid(row=0, column=2, padx=(8, 0))

        ttk.Label(options, text="缓存总量上限").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(10, 0))
        size_row = ttk.Frame(options)
        size_row.grid(row=1, column=1, sticky="w", pady=(10, 0))
        self.budget_var = tk.StringVar(value="10")
        self.budget_spin = ttk.Spinbox(
            size_row, from_=1, to=200, increment=1, width=8, textvariable=self.budget_var
        )
        self.budget_spin.pack(side="left")
        ttk.Label(size_row, text="GB", style="Hint.TLabel").pack(side="left", padx=(6, 0))

        ttk.Label(options, text="单文件体积上限").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=(10, 0))
        file_row = ttk.Frame(options)
        file_row.grid(row=2, column=1, sticky="w", pady=(10, 0))
        self.max_file_var = tk.StringVar(value="4")
        self.max_file_spin = ttk.Spinbox(
            file_row, from_=1, to=4096, increment=1, width=8, textvariable=self.max_file_var
        )
        self.max_file_spin.pack(side="left")
        ttk.Label(file_row, text="MB（只预热小于该值的文件）", style="Hint.TLabel").pack(side="left", padx=(6, 0))

        action = ttk.Frame(outer)
        action.pack(fill="x", pady=(14, 0))
        self.start_button = ttk.Button(action, text="扫描并预热", command=self._on_start)
        self.start_button.pack(side="left")
        ttk.Label(
            action,
            text="预热完成后请尽快启动游戏本体，间隔越短，缓存越不容易被系统回收。",
            style="Hint.TLabel",
        ).pack(side="left", padx=(12, 0))

        progress = ttk.Frame(outer)
        progress.pack(fill="x", pady=(12, 4))
        self.progress_bar = ttk.Progressbar(progress, mode="determinate", maximum=1.0)
        self.progress_bar.pack(fill="x")
        self.status_var = tk.StringVar(value="等待开始")
        self.detail_var = tk.StringVar(value="")
        status_row = ttk.Frame(outer)
        status_row.pack(fill="x")
        ttk.Label(status_row, textvariable=self.status_var).pack(side="left")
        ttk.Label(status_row, textvariable=self.detail_var, style="Hint.TLabel").pack(side="right")

        log_frame = ttk.LabelFrame(outer, text="运行日志", padding=8)
        log_frame.pack(fill="both", expand=True, pady=(12, 0))
        log_header = ttk.Frame(log_frame)
        log_header.pack(fill="x", pady=(0, 6))
        ttk.Button(log_header, text="清空日志", command=self._clear_log).pack(side="right")
        self.log_view = ScrolledText(log_frame, height=14, wrap="word", state="disabled", borderwidth=0)
        self.log_view.pack(fill="both", expand=True)

    # ---------- 交互 ----------

    def _browse(self) -> None:
        path = filedialog.askdirectory(title="选择游戏安装目录")
        if path:
            self.folder_var.set(os.path.normpath(path))

    def _on_start(self) -> None:
        if self.busy:
            return
        folder = self.folder_var.get().strip()
        if not folder or not os.path.isdir(folder):
            messagebox.showerror("路径无效", "请选择有效的游戏安装目录。")
            return
        try:
            budget_gb = float(self.budget_var.get())
            max_file_mb = float(self.max_file_var.get())
        except ValueError:
            messagebox.showerror("参数无效", "缓存上限与单文件上限需要填写数字。")
            return
        if budget_gb <= 0 or max_file_mb <= 0:
            messagebox.showerror("参数无效", "缓存上限与单文件上限需要大于 0。")
            return

        self._log("")
        self._log(f"目标目录：{folder}")
        self._log(f"预算 {budget_gb:g} GB，单文件上限 {max_file_mb:g} MB")
        self._set_busy(True)
        self._progress_busy()
        threading.Thread(
            target=self._scan_worker,
            args=(folder, int(budget_gb * GB), int(max_file_mb * MB)),
            daemon=True,
        ).start()

    # ---------- 后台任务 ----------

    def _scan_worker(self, folder: str, budget: int, max_size: int) -> None:
        try:
            candidates, stats = scan(
                folder,
                max_size,
                on_stage=lambda text: self._post("stage", text),
                on_progress=lambda done, total, text: self._post("progress", done, total, text),
            )
            ordered, selected, selected_bytes, dropped = build_plan(candidates, budget)
            self._post(
                "scan_done",
                {
                    "files_seen": stats.files_seen,
                    "matched_files": stats.matched_files,
                    "matched_bytes": stats.matched_bytes,
                    "lcn_missing": stats.lcn_missing,
                    "elapsed": stats.elapsed,
                    "budget": budget,
                    "selected": selected,
                    "selected_bytes": selected_bytes,
                    "dropped": dropped,
                    "ordered": len(ordered),
                },
            )
        except Exception as exc:  # 扫描阶段的任何异常都要回到界面
            self._post("error", f"扫描失败：{exc}")

    def _warm_worker(self, selected) -> None:
        try:
            stats = warm(
                selected,
                on_progress=lambda done, total, files_done, files_total, path, elapsed: self._post(
                    "warm_progress", done, total, files_done, files_total, path, elapsed
                ),
            )
            self._post("done", stats)
        except Exception as exc:
            self._post("error", f"预热失败：{exc}")

    # ---------- 事件循环 ----------

    def _post(self, kind: str, *payload) -> None:
        self.events.put((kind, payload))

    def _poll_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                self._handle_event(kind, *payload)
        except queue.Empty:
            pass
        self.after(80, self._poll_events)

    def _handle_event(self, kind: str, *payload) -> None:
        if kind == "stage":
            self.status_var.set(payload[0])
        elif kind == "progress":
            done, total, text = payload
            if total > 0:
                self._progress_determinate(done / total)
            self.status_var.set(text)
        elif kind == "scan_done":
            self._on_scan_done(payload[0])
        elif kind == "warm_progress":
            self._on_warm_progress(*payload)
        elif kind == "done":
            self._on_done(payload[0])
        elif kind == "error":
            self._progress_determinate(0.0)
            self._set_busy(False)
            self.status_var.set("出错")
            self._log(payload[0])
            messagebox.showerror("出错了", payload[0])

    def _on_scan_done(self, info: dict) -> None:
        self._progress_determinate(1.0)
        self._log(
            f"遍历 {info['files_seen']} 个文件，命中 {info['matched_files']} 个，"
            f"共 {human_bytes(info['matched_bytes'])}，用时 {info['elapsed']:.1f} 秒"
        )
        if info["lcn_missing"]:
            self._log(
                f"其中 {info['lcn_missing']} 个文件拿不到物理位置"
                f"（数据常驻 MFT 的极小文件，或卷不是 NTFS），已按 MFT 序号排在末尾。"
            )
        self._log(
            f"按物理扇区顺序排序完成，预算 {human_bytes(info['budget'])}："
            f"计划预热 {len(info['selected'])} 个文件 / {human_bytes(info['selected_bytes'])}，"
            f"截断丢弃 {info['dropped']} 个"
        )

        if not info["selected"]:
            self._set_busy(False)
            self.status_var.set("没有可预热的文件")
            messagebox.showwarning("没有可预热的文件", "没有符合条件的文件，或者缓存总量上限小于单个文件。")
            return

        self._set_busy(False)
        self.status_var.set("等待确认预热计划")
        answer = messagebox.askokcancel(
            "确认预热计划",
            f"候选文件：{info['matched_files']} 个，共 {human_bytes(info['matched_bytes'])}\n"
            f"缓存上限：{human_bytes(info['budget'])}\n\n"
            f"将预热：{len(info['selected'])} 个文件，共 {human_bytes(info['selected_bytes'])}\n"
            f"因超出上限丢弃：{info['dropped']} 个（从物理位置靠后的文件开始丢弃）\n\n"
            f"是否开始读取？",
        )
        if not answer:
            self.status_var.set("已取消")
            self._log("用户取消了预热。")
            return

        self._set_busy(True)
        self._progress_determinate(0.0)
        self.status_var.set("预热中…")
        self._log("开始按物理扇区顺序串行读取，数据读入缓存后即丢弃。")
        threading.Thread(target=self._warm_worker, args=(info["selected"],), daemon=True).start()

    def _on_warm_progress(
        self, done: int, total: int, files_done: int, files_total: int, path: str, elapsed: float
    ) -> None:
        self._progress_determinate(done / total if total else 0.0)
        speed = done / elapsed / MB if elapsed > 0 else 0.0
        eta = (total - done) / (done / elapsed) if done > 0 and elapsed > 0 else 0.0
        self.status_var.set(f"预热中 {files_done}/{files_total} · {os.path.basename(path)}")
        self.detail_var.set(
            f"{human_bytes(done)} / {human_bytes(total)}  ·  {speed:.1f} MB/s  ·  剩余 {human_duration(eta)}"
        )

    def _on_done(self, stats) -> None:
        self._set_busy(False)
        self._progress_determinate(1.0)
        self.status_var.set("预热完成")
        self.detail_var.set(
            f"{human_bytes(stats.bytes_read)}  ·  {stats.avg_mbps:.1f} MB/s  ·  用时 {human_duration(stats.elapsed)}"
        )
        self._log(
            f"预热完成：{stats.files_done} 个文件 / {human_bytes(stats.bytes_read)}，"
            f"用时 {human_duration(stats.elapsed)}，平均 {stats.avg_mbps:.1f} MB/s"
        )
        if stats.files_failed:
            self._log(f"有 {stats.files_failed} 个文件读取失败（被占用或权限不足），已跳过。")
        messagebox.showinfo(
            "预热完成",
            "游戏文件已读入系统页面缓存。\n\n"
            "请立即启动游戏本体，不要先做其他占用磁盘和内存的操作——\n"
            "间隔越短，缓存被系统回收的概率越低。",
        )

    # ---------- 小工具 ----------

    def _set_busy(self, busy: bool) -> None:
        self.busy = busy
        state = "disabled" if busy else "normal"
        self.start_button.configure(state=state)
        self.browse_button.configure(state=state)
        self.folder_entry.configure(state=state)
        self.budget_spin.configure(state=state)
        self.max_file_spin.configure(state=state)

    def _progress_busy(self) -> None:
        self.progress_bar.stop()
        self.progress_bar.configure(mode="indeterminate", maximum=1.0)
        self.progress_bar.start(12)

    def _progress_determinate(self, fraction: float) -> None:
        if str(self.progress_bar.cget("mode")) != "determinate":
            self.progress_bar.stop()
            self.progress_bar.configure(mode="determinate", maximum=1.0)
        self.progress_bar["value"] = max(0.0, min(1.0, fraction))

    def _log(self, text: str) -> None:
        if not text:
            self.log_view.configure(state="normal")
            self.log_view.insert("end", "\n")
            self.log_view.configure(state="disabled")
            self.log_view.see("end")
            return
        self.log_view.configure(state="normal")
        self.log_view.insert("end", f"[{time.strftime('%H:%M:%S')}] {text}\n")
        self.log_view.configure(state="disabled")
        self.log_view.see("end")

    def _clear_log(self) -> None:
        self.log_view.configure(state="normal")
        self.log_view.delete("1.0", "end")
        self.log_view.configure(state="disabled")


# =============================================================================
# 五、入口
# =============================================================================


def main() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    app = PrewarmerApp()
    app.mainloop()


if __name__ == "__main__":
    main()
