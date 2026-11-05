"""Windowsで、GUIから実行したときに FFmpeg などの黒いコンソール画面が開かないようにする。

あわせて、このプロセスが起動した FFmpeg などを「ジョブ」にまとめ、このプロセスが終わったとき
（GUI の「中止」で止められたときも含む）に一緒に終わるようにする。
"""
import os
import subprocess

if os.name == "nt":
    _orig_init = subprocess.Popen.__init__
    _job = None

    def _make_job():
        """終了時に中のプロセスをすべて止める Job Object を作る（失敗したら None）"""
        try:
            import ctypes
            from ctypes import wintypes

            k32 = ctypes.WinDLL("kernel32", use_last_error=True)

            class IO_COUNTERS(ctypes.Structure):
                _fields_ = [(n, ctypes.c_ulonglong) for n in (
                    "ReadOperationCount", "WriteOperationCount", "OtherOperationCount",
                    "ReadTransferCount", "WriteTransferCount", "OtherTransferCount")]

            class BASIC(ctypes.Structure):
                _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                            ("LimitFlags", wintypes.DWORD), ("MinimumWorkingSetSize", ctypes.c_size_t),
                            ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", wintypes.DWORD),
                            ("Affinity", ctypes.c_size_t), ("PriorityClass", wintypes.DWORD),
                            ("SchedulingClass", wintypes.DWORD)]

            class EXTENDED(ctypes.Structure):
                _fields_ = [("BasicLimitInformation", BASIC), ("IoInfo", IO_COUNTERS),
                            ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                            ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]

            k32.CreateJobObjectW.restype = wintypes.HANDLE
            k32.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
            k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD]
            k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
            h = k32.CreateJobObjectW(None, None)
            if not h:
                return None
            info = EXTENDED()
            info.BasicLimitInformation.LimitFlags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            if not k32.SetInformationJobObject(h, 9, ctypes.byref(info), ctypes.sizeof(info)):
                return None
            return k32, h
        except Exception:  # noqa: BLE001
            return None

    def _no_window_init(self, *args, **kwargs):
        global _job
        kwargs.setdefault("creationflags", 0x08000000)  # CREATE_NO_WINDOW
        _orig_init(self, *args, **kwargs)
        try:
            if _job is None:
                _job = _make_job() or False
            if _job:
                k32, h = _job
                k32.AssignProcessToJobObject(h, int(self._handle))
        except Exception:  # noqa: BLE001  ジョブに入れられなくても、処理自体は続ける
            pass

    subprocess.Popen.__init__ = _no_window_init
