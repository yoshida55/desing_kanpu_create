"""
常駐ランチャー ― ホットキー1発で「デザイン検索の窓」を呼び出す（Windows専用）。

【何を解決するか】
  ① 起動が面倒       → PCに常駐させ、Ctrl+Alt+D で一瞬で出す（サーバは裏でずっと起きている）
  ② タブが増える     → 窓は「1枚だけ」使い回す。既に開いていれば新しく開かずに前面に出すだけ
  ③ 検索が遅い       → サーバを起こしっぱなしにすると、AIモデルが読み込み済みのまま残る
                        （毎回起動し直すと、初回検索でモデル読み込みの待ちが必ず発生していた）

【使い方】
  常駐起動.bat をダブルクリック → 画面右下のトレイに虫めがねアイコンが出る
  Ctrl+Alt+D          … 検索窓を出す／出ている時はしまう（トグル）
  Ctrl+Alt+Shift+D    … 常駐を終了する（トレイからも終了できる）

【設計メモ（触るときの注意）】
  ★ホットキーは Windows の RegisterHotKey（OS標準の仕組み）で取る。キーを監視し続ける
    ライブラリ（keyboard 等）と違って管理者権限が要らず、取りこぼしもない。
  ★キーやポートの値はここに直書きせず src/config.py の QuickConfig に置く（このプロジェクトの約束）。
  ★サーバを「自分で起動した時だけ」止める。既に 起動.bat で立っているサーバは絶対に止めない
    （ユーザーが作業中のサーバを巻き添えで落とす事故を防ぐため・CLAUDE.md §7㊴）。
"""

from __future__ import annotations

import ctypes
import os
import socket
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# .env を読む（cli.py と同じ流儀。読まないと DESIGN_STOCK_* の設定が効かない）
try:
    from dotenv import load_dotenv

    load_dotenv(ROOT / ".env", override=True)
except Exception:  # noqa: BLE001
    pass

from src import config  # noqa: E402

CFG = config.CONFIG.quick

LOG_PATH = config.DATA_DIR / "quick_launcher.log"
SERVER_LOG_PATH = config.DATA_DIR / "quick_launcher_server.log"

# ── Windows API ────────────────────────────────────────────────
user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WM_HOTKEY = 0x0312
MOD_NOREPEAT = 0x4000
SW_RESTORE = 9
SW_SHOW = 5
SW_MINIMIZE = 6
VK_MENU = 0x12
KEYEVENTF_KEYUP = 0x0002
CREATE_NO_WINDOW = 0x08000000
ERROR_ALREADY_EXISTS = 183
SPI_GETFOREGROUNDLOCKTIMEOUT = 0x2000
SPI_SETFOREGROUNDLOCKTIMEOUT = 0x2001

user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
user32.RegisterHotKey.restype = wintypes.BOOL
user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT]
user32.GetMessageW.restype = ctypes.c_int
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
# ★ここを省略すると 64bit では引数・戻り値が 32bit に切り詰められ、
#   ウィンドウ番号(HWND)の比較が狂う＝「前面にいるか」の判定が効かなくなる。
user32.GetForegroundWindow.restype = wintypes.HWND
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.BringWindowToTop.argtypes = [wintypes.HWND]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.IsIconic.argtypes = [wintypes.HWND]
user32.SwitchToThisWindow.argtypes = [wintypes.HWND, wintypes.BOOL]
user32.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT, ctypes.c_void_p, wintypes.UINT]

WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]

# ── ホットキー文字列 → 修飾キー＋キーコード ──────────────────
_MODS = {"alt": 0x0001, "ctrl": 0x0002, "control": 0x0002, "shift": 0x0004, "win": 0x0008}
_VKS = {
    "space": 0x20, "enter": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
    "insert": 0x2D, "delete": 0x2E, "home": 0x24, "end": 0x23,
    "pageup": 0x21, "pagedown": 0x22, "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    **{f"f{i}": 0x6F + i for i in range(1, 13)},  # F1..F12
}


def parse_hotkey(text: str) -> tuple[int, int]:
    """"ctrl+alt+d" → (修飾キーのビット, キーコード)。おかしければ例外を投げる。"""
    mods = 0
    vk = 0
    for part in [p.strip().lower() for p in text.split("+") if p.strip()]:
        if part in _MODS:
            mods |= _MODS[part]
        elif part in _VKS:
            vk = _VKS[part]
        elif len(part) == 1:
            vk = ord(part.upper())
        else:
            raise ValueError(f"知らないキーです: {part}")
    if not vk:
        raise ValueError(f"キーが指定されていません: {text}")
    return mods | MOD_NOREPEAT, vk


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}  {msg}"
    print(line, flush=True)
    try:
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:  # noqa: BLE001
        pass


# ── サーバ（Flask）の面倒を見る ────────────────────────────────
_server_proc: subprocess.Popen | None = None  # 自分で起動した時だけ入る
_server_log_file = None


def server_alive(timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((CFG.host, CFG.port), timeout=timeout):
            return True
    except OSError:
        return False


def start_server() -> bool:
    """サーバが立っていなければ裏で起動する。既に立っていれば何もしない。"""
    global _server_proc, _server_log_file
    if server_alive():
        return True
    py = ROOT / "venv" / "Scripts" / "python.exe"
    if not py.exists():
        log(f"venv が見つかりません: {py}")
        return False
    SERVER_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    _server_log_file = SERVER_LOG_PATH.open("ab")  # ★閉じない（子プロセスが書き続けるため）
    # --no-preload：窓をすぐ出せるように、モデルは起動5秒後にバックグラウンドで読む
    #               （viewer.serve 側にその仕組みがある）
    cmd = [str(py), "cli.py", "serve", "--no-preload", "--no-open",
           "--host", CFG.host, "--port", str(CFG.port)]
    log(f"サーバを起動します: {' '.join(cmd)}")
    _server_proc = subprocess.Popen(
        cmd, cwd=str(ROOT), stdout=_server_log_file, stderr=subprocess.STDOUT,
        creationflags=CREATE_NO_WINDOW,
    )
    return True


def wait_server(limit_s: float = 40.0) -> bool:
    """サーバが応答するまで待つ（起動直後は数秒かかる）。"""
    end = time.time() + limit_s
    while time.time() < end:
        if server_alive():
            return True
        time.sleep(0.3)
    return False


def stop_server_if_mine() -> None:
    """自分で起動したサーバだけ止める。ユーザーが 起動.bat で立てたものには触らない。"""
    global _server_proc
    if _server_proc and _server_proc.poll() is None:
        log("自分で起動したサーバを停止します")
        try:
            _server_proc.terminate()
        except Exception:  # noqa: BLE001
            pass
    _server_proc = None


# ── 窓を探す・前に出す ────────────────────────────────────────
def find_window(mark: str) -> int | None:
    """タイトルに mark を含む「見えている窓」を探す（＝既に開いている検索窓）。"""
    found: list[int] = []

    def _cb(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        n = user32.GetWindowTextLengthW(hwnd)
        if n <= 0:
            return True
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(hwnd, buf, n + 1)
        if mark in buf.value:
            found.append(hwnd)
            return False  # 最初の1枚で打ち切り
        return True

    user32.EnumWindows(WNDENUMPROC(_cb), 0)
    return found[0] if found else None


def force_foreground(hwnd: int) -> None:
    """最前面に持ってくる。

    ★SetForegroundWindow は「今 前面にいるアプリ」以外から呼ぶと Windows に無視されることがある。
      前面アプリのスレッドに一時的に自分をくっつける（AttachThreadInput）＋ALTキーの空打ちで、
      OSに「ユーザー操作だ」と認めさせるのが定番の回避策。
    """
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    else:
        user32.ShowWindow(hwnd, SW_SHOW)
    fg = user32.GetForegroundWindow()
    cur_tid = kernel32.GetCurrentThreadId()
    fg_tid = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    attached = bool(fg_tid) and fg_tid != cur_tid
    if attached:
        user32.AttachThreadInput(cur_tid, fg_tid, True)
    try:
        # ★Windows は「直前に別アプリを操作していると前面化を拒否する」ルール
        #   （フォアグラウンドロック）を持つ。その待ち時間を一時的に0にして確実に前へ出す。
        prev = wintypes.UINT(0)
        got = user32.SystemParametersInfoW(SPI_GETFOREGROUNDLOCKTIMEOUT, 0, ctypes.byref(prev), 0)
        if got:
            user32.SystemParametersInfoW(SPI_SETFOREGROUNDLOCKTIMEOUT, 0, ctypes.c_void_p(0), 0)
        user32.keybd_event(VK_MENU, 0, 0, 0)
        user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        # それでも前に来ない時の最後の手段（タスクバーのサムネイルが使っている呼び出し）
        for _ in range(3):
            if user32.GetForegroundWindow() == hwnd:
                break
            user32.SwitchToThisWindow(hwnd, True)
            time.sleep(0.06)
        if got:
            user32.SystemParametersInfoW(
                SPI_SETFOREGROUNDLOCKTIMEOUT, 0, ctypes.c_void_p(prev.value), 0)
    finally:
        if attached:
            user32.AttachThreadInput(cur_tid, fg_tid, False)


def find_browser() -> Path | None:
    """アプリモード（タブなしの単独窓）で開けるブラウザを探す。Chrome → Edge の順。"""
    candidates = [
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / r"Google\Chrome\Application\chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / r"Google\Chrome\Application\chrome.exe",
        Path(os.environ.get("LOCALAPPDATA", "")) / r"Google\Chrome\Application\chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")) / r"Microsoft\Edge\Application\msedge.exe",
        Path(os.environ.get("PROGRAMFILES", r"C:\Program Files")) / r"Microsoft\Edge\Application\msedge.exe",
    ]
    for p in candidates:
        if p.is_file():
            return p
    return None


def open_window() -> None:
    """検索窓を新しく開く。★アプリモード（--app）で開くのでタブ列が無い＝タブが増えない。"""
    exe = find_browser()
    if exe is None:
        log("Chrome/Edge が見つからないので既定のブラウザで開きます（タブとして開きます）")
        import webbrowser

        webbrowser.open(CFG.url)
        return
    subprocess.Popen([
        str(exe),
        f"--app={CFG.url}",
        f"--window-size={CFG.window_w},{CFG.window_h}",
    ])


_busy = threading.Lock()
ACTIVE_HOTKEY = ""  # 実際に登録できたキー（希望のキーが取られていた場合は予備が入る）


def toggle_window() -> None:
    """ホットキーを押したときの本体。出ていれば前へ／前にいればしまう／無ければ開く。"""
    if not _busy.acquire(blocking=False):
        return  # 連打対策（起動待ちの最中に何枚も開かない）
    try:
        hwnd = find_window(CFG.window_title_mark)
        if hwnd:
            if user32.GetForegroundWindow() == hwnd:
                user32.ShowWindow(hwnd, SW_MINIMIZE)  # もう一度押したらしまう
            else:
                force_foreground(hwnd)
            # ★窓は残っているのにサーバだけ落ちている場合がある（PCのスリープ後など）。
            #   前に出すだけだと「開いたのに検索できない」になるので、裏で起こし直しておく。
            if not server_alive():
                log("窓は開いていますが、サーバが落ちていたので起こし直します（画面はF5で戻ります）")
                threading.Thread(target=start_server, daemon=True).start()
            return
        if not server_alive():
            start_server()
            if not wait_server():
                log("サーバが応答しませんでした（data/quick_launcher_server.log を確認）")
                return
        open_window()
    except Exception as exc:  # noqa: BLE001
        log(f"窓の呼び出しで失敗: {exc}")
    finally:
        _busy.release()


# ── トレイアイコン（あれば出す。無くても本体は動く） ──────────
_tray = None


def _tray_image():
    from PIL import Image, ImageDraw

    img = Image.new("RGBA", (64, 64), (24, 28, 34, 255))
    d = ImageDraw.Draw(img)
    d.ellipse((14, 12, 44, 42), outline=(120, 220, 180, 255), width=6)  # 虫めがねの輪
    d.line((40, 38, 54, 52), fill=(120, 220, 180, 255), width=7)        # 柄
    return img


def start_tray() -> None:
    global _tray
    try:
        import pystray
    except Exception:  # noqa: BLE001
        log("pystray が無いのでトレイアイコンなしで動かします（ホットキーは使えます）")
        return

    def _quit(icon, _item):
        icon.visible = False
        icon.stop()
        os._exit(0)

    def _quit_all(icon, _item):
        stop_server_if_mine()
        icon.visible = False
        icon.stop()
        os._exit(0)

    menu = pystray.Menu(
        pystray.MenuItem(f"検索窓を出す（{(ACTIVE_HOTKEY or CFG.hotkey).upper()}）",
                         lambda *_: toggle_window(), default=True),
        pystray.MenuItem("サーバのログを開く", lambda *_: os.startfile(SERVER_LOG_PATH) if SERVER_LOG_PATH.exists() else None),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("常駐を終了（サーバは動かしたまま）", _quit),
        pystray.MenuItem("サーバごと終了", _quit_all),
    )
    _tray = pystray.Icon("design_stock_quick", _tray_image(), "デザイン検索（常駐）", menu)
    threading.Thread(target=_tray.run, daemon=True).start()
    time.sleep(0.6)
    try:
        _tray.notify(f"{(ACTIVE_HOTKEY or CFG.hotkey).upper()} で検索窓が出ます", "デザイン検索を常駐しました")
    except Exception:  # noqa: BLE001
        pass


# ── 本体 ──────────────────────────────────────────────────────
def main() -> int:
    # 二重起動の防止（同じ常駐が2つ動くとホットキーの取り合いになる）
    # ★エラー番号は ctypes.get_last_error() で取ること。kernel32.GetLastError() を直接呼ぶと、
    #   use_last_error=True のせいで ctypes が退避した別の値が返り、いつも 0＝素通りになる。
    handle = kernel32.CreateMutexW(None, False, "Local\\DesignStockQuickLauncher")
    if ctypes.get_last_error() == ERROR_ALREADY_EXISTS:
        log("既に常駐しています（二重起動はしません）")
        return 0
    _ = handle  # 参照を保持（ガベージコレクトでミューテックスが消えないように）

    # ★希望のキーが他アプリに取られていたら、予備のキーへ順に逃げる。
    #   逃げないと「常駐しているのに何も起きない」＝一番わかりにくい壊れ方になる。
    global ACTIVE_HOTKEY
    for name in [CFG.hotkey] + [k.strip() for k in CFG.hotkey_alts.split(",") if k.strip()]:
        try:
            mods, vk = parse_hotkey(name)
        except ValueError as exc:
            log(f"ホットキーの指定が不正なので飛ばします（{name}）: {exc}")
            continue
        if user32.RegisterHotKey(None, 1, mods, vk):
            ACTIVE_HOTKEY = name
            break
        log(f"{name} は他のアプリが使っているので、次の候補を試します")
    if not ACTIVE_HOTKEY:
        log("使えるホットキーがありませんでした。.env の DESIGN_STOCK_HOTKEY で指定してください")
        return 1

    try:
        quit_mods, quit_vk = parse_hotkey(CFG.quit_hotkey)
        user32.RegisterHotKey(None, 2, quit_mods, quit_vk)  # 終了キーは取れなくても続行
    except ValueError:
        pass

    log(f"常駐しました。{ACTIVE_HOTKEY.upper()} で検索窓 / {CFG.quit_hotkey.upper()} で終了")
    start_tray()

    # サーバを先に起こしておく＝初回のホットキーで待たされない＋モデルが温まる
    threading.Thread(target=start_server, daemon=True).start()

    msg = wintypes.MSG()
    while True:
        ret = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
        if ret in (0, -1):
            break
        if msg.message == WM_HOTKEY:
            if msg.wParam == 1:
                threading.Thread(target=toggle_window, daemon=True).start()
            elif msg.wParam == 2:
                log("終了キーが押されました")
                break
    user32.UnregisterHotKey(None, 1)
    user32.UnregisterHotKey(None, 2)
    if _tray is not None:
        try:
            _tray.stop()
        except Exception:  # noqa: BLE001
            pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
