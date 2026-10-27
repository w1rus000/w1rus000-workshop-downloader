import subprocess
import time
import os
import shutil
import traceback
import threading
import queue
import tkinter as tk
from tkinter import ttk, messagebox, filedialog
import ctypes
import sys
import json
import tempfile

try:
    import requests
except ImportError:
    with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "error.log"),
              "a", encoding="utf-8") as error_file:
        traceback.print_exc(file=error_file)
    raise

# ─── Acrylic / Transparency helpers (Windows 10/11) ───────────────────────────
def set_acrylic(hwnd, accent_color=0x00000000):
    """Etkinleştirmek için Windows Acrylic blur efekti uygular."""
    try:
        ACCENT_ENABLE_ACRYLICBLURBEHIND = 4
        GWL_EXSTYLE = -20
        WS_EX_LAYERED = 0x80000

        class ACCENT_POLICY(ctypes.Structure):
            _fields_ = [
                ("AccentState", ctypes.c_uint),
                ("AccentFlags", ctypes.c_uint),
                ("GradientColor", ctypes.c_uint),
                ("AnimationId", ctypes.c_uint),
            ]

        class WINDOWCOMPOSITIONATTRIBDATA(ctypes.Structure):
            _fields_ = [
                ("Attribute", ctypes.c_uint),
                ("Data", ctypes.POINTER(ctypes.c_int)),
                ("SizeOfData", ctypes.c_size_t),
            ]

        accent = ACCENT_POLICY()
        accent.AccentState = ACCENT_ENABLE_ACRYLICBLURBEHIND
        accent.AccentFlags = 2
        accent.GradientColor = accent_color  # AABBGGRR

        data = WINDOWCOMPOSITIONATTRIBDATA()
        data.Attribute = 19  # WCA_ACCENT_POLICY
        data.SizeOfData = ctypes.sizeof(accent)
        data.Data = ctypes.cast(ctypes.pointer(accent), ctypes.POINTER(ctypes.c_int))

        user32 = ctypes.windll.user32
        user32.SetWindowCompositionAttribute(hwnd, ctypes.pointer(data))

        # WS_EX_LAYERED ekle
        ex_style = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, ex_style | WS_EX_LAYERED)
    except Exception:
        pass  # Windows 7/8 gibi eski sürümlerde sessizce geç


# ─── Steam API Functions ───────────────────────────────────────────────────────
def get_collection_items(collection_id):
    url = "https://api.steampowered.com/ISteamRemoteStorage/GetCollectionDetails/v1/"
    data = {
        "collectioncount": 1,
        "publishedfileids[0]": collection_id
    }
    try:
        response = requests.post(url, data=data, timeout=15)
        if response.status_code == 200:
            json_data = response.json()
            try:
                details = json_data['response']['collectiondetails'][0]
                if details.get('result') != 1:
                    return [], "Koleksiyon bulunamadı veya gizli."
                if 'children' in details:
                    children = details['children']
                    items = list(dict.fromkeys(str(child['publishedfileid'])
                                 for child in children if child.get('filetype', 0) == 0))
                    if any(child.get('filetype', 0) != 0 for child in children):
                        return items, "İç içe koleksiyon desteklenmiyor. Alt koleksiyonları ayrı indirin."
                    if not items:
                        return [], "Koleksiyon boş."
                    return items, None
                else:
                    return [], "Koleksiyon boş, gizli (private) veya hatalı ID."
            except (KeyError, IndexError, TypeError):
                return [], "API yanıtı beklenmeyen formatta."
        else:
            return [], f"API isteği başarısız. Hata kodu: {response.status_code}"
    except Exception as e:
        return [], f"İnternet veya API bağlantısında sorun: {e}"


def get_items_details(item_ids, log_callback=None):
    if not item_ids:
        return {}
    url = "https://api.steampowered.com/ISteamRemoteStorage/GetPublishedFileDetails/v1/"
    details_map = {}
    
    if log_callback:
        log_callback("[*] Mod başlıkları API'den sorgulanıyor...", "dim")
        
    chunk_size = 50
    for i in range(0, len(item_ids), chunk_size):
        chunk = item_ids[i:i + chunk_size]
        data = {
            "itemcount": len(chunk)
        }
        for idx, item_id in enumerate(chunk):
            data[f"publishedfileids[{idx}]"] = item_id
            
        try:
            response = requests.post(url, data=data, timeout=10)
            if response.status_code == 200:
                res_json = response.json()
                details = res_json.get("response", {}).get("publishedfiledetails", [])
                for detail in details:
                    fid = detail.get("publishedfileid")
                    title = detail.get("title")
                    if fid and title:
                        details_map[str(fid)] = title
        except Exception as e:
            if log_callback:
                log_callback(f"[!] Başlık sorgulama hatası: {e}", "warning")
            
    return details_map


def replace_mod_directory(src, dest):
    """Stage a complete copy before replacing an existing mod; roll back on failure."""
    parent = os.path.realpath(os.path.dirname(os.path.abspath(dest)))
    dest = os.path.abspath(dest)
    if os.path.islink(dest) or os.path.realpath(dest) != dest:
        raise OSError("Hedef klasör bağlantı içeriyor; güvenli değiştirme yapılamadı.")
    staging = tempfile.mkdtemp(prefix=".mod-transfer-", dir=parent)
    preserve_backup = False
    try:
        fresh = os.path.join(staging, "new")
        backup = os.path.join(staging, "previous")
        shutil.copytree(src, fresh)
        had_previous = os.path.exists(dest)
        if had_previous:
            os.replace(dest, backup)
        try:
            os.replace(fresh, dest)
        except OSError:
            if had_previous:
                try:
                    os.replace(backup, dest)
                except OSError:
                    preserve_backup = True
                    raise OSError(f"Önceki mod yedeği korundu: {backup}")
            raise
    finally:
        # Only remove the staging directory created by this call.
        if not preserve_backup:
            shutil.rmtree(staging)


def download_mods(app_id, item_ids, target_dir, steamcmd_path, log_callback,
                  progress_callback, stop_event=None,
                  username="", password="", steam_guard="", titles_map=None):
    if not item_ids:
        log_callback("[-] İndirilecek mod bulunamadı.", "error")
        return False
    if not str(app_id).isascii() or not str(app_id).isdigit() or any(
            not str(mid).isascii() or not str(mid).isdigit() for mid in item_ids):
        log_callback("[-] App ID ve mod ID değerleri sayısal olmalı.", "error")
        return False

    target_dir = os.path.abspath(target_dir)
    steamcmd_path = os.path.abspath(steamcmd_path)
    os.makedirs(target_dir, exist_ok=True)
    stop_event = stop_event if stop_event is not None else threading.Event()
    all_ok = True
    completed = 0
    downloaded = set()
    log_callback(f"[*] İndirme başlatılıyor → {target_dir}", "info")

    for i in range(0, len(item_ids), 20):
        if stop_event.is_set():
            return False
        chunk = item_ids[i:i + 20]
        login_args = ["+login", username] if username else ["+login", "anonymous"]
        if username and password:
            login_args.append(password)
        if username and password and steam_guard:
            login_args.append(steam_guard)
        args = [steamcmd_path, "+force_install_dir", target_dir] + login_args
        for mid in chunk:
            args.extend(["+workshop_download_item", str(app_id), str(mid)])
        args.append("+quit")
        confirmed = set()
        proc = None
        watcher_done = threading.Event()
        watcher = None
        try:
            si = subprocess.STARTUPINFO()
            si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
            proc = subprocess.Popen(
                args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, encoding="utf-8", errors="replace",
                startupinfo=si, creationflags=subprocess.CREATE_NO_WINDOW,
                cwd=os.path.dirname(steamcmd_path))

            # Reading stdout may block; cancellation must run independently.
            def cancel_process():
                while not watcher_done.wait(0.1):
                    if stop_event.is_set():
                        try:
                            proc.terminate()
                            proc.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            proc.kill()
                        except OSError:
                            pass
                        return

            watcher = threading.Thread(target=cancel_process, daemon=True)
            watcher.start()
            for line in proc.stdout:
                line = line.rstrip()
                if line:
                    if "progress:" not in line.lower():
                        safe_line = line
                        for secret in (password, steam_guard):
                            if secret:
                                safe_line = safe_line.replace(secret, "***")
                        log_callback(safe_line, "dim")
                    if "Success. Downloaded item " in line:
                        mid = line.split("Success. Downloaded item ", 1)[1].split()[0].strip('". ')
                        if mid in {str(value) for value in chunk}:
                            confirmed.add(mid)
                            downloaded.add(mid)
                            progress_callback(min(99, int(len(downloaded) / len(item_ids) * 100)))
            proc.wait()
            if stop_event.is_set():
                log_callback("[■] İndirme durduruldu.", "warning")
                return False
            if proc.returncode != 0:
                all_ok = False
                log_callback(f"[-] SteamCMD çıkış kodu: {proc.returncode}", "error")

            for mid in chunk:
                mid = str(mid)
                src = os.path.join(target_dir, "steamapps", "workshop", "content", str(app_id), mid)
                dest = os.path.join(target_dir, mid)
                if mid not in confirmed or not os.path.isdir(src):
                    all_ok = False
                    log_callback(f"[-] {mid}: İndirme doğrulanamadı; dosyalar korundu.", "error")
                    continue
                try:
                    # Copy first: a failed transfer must not destroy the previous download.
                    replace_mod_directory(src, dest)
                    completed += 1
                except OSError as exc:
                    all_ok = False
                    log_callback(f"[-] {mid} kopyalama hatası: {exc}", "error")
        except Exception as exc:
            all_ok = False
            log_callback(f"[-] İndirme hatası: {exc}", "error")
        finally:
            watcher_done.set()
            if proc is not None:
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        proc.wait()
                if watcher is not None:
                    watcher.join(timeout=6)
                proc.stdout.close()

    if all_ok and completed == len(item_ids):
        progress_callback(100)
        log_callback(f"[✓] Tüm modlar indirildi → {target_dir}", "success")
        return True
    log_callback(f"[-] İndirme eksik: {completed}/{len(item_ids)} mod doğrulandı.", "error")
    return False


# ─── GUI Application ───────────────────────────────────────────────────────────
class SteamDownloaderApp:
    # Renk paleti
    BG          = "#0d0f14"
    SURFACE     = "#161b27"
    SURFACE2    = "#1e2535"
    ACCENT      = "#4f8ef7"
    ACCENT2     = "#6fd6ff"
    TEXT        = "#e8eaf0"
    TEXT_DIM    = "#7a8299"
    SUCCESS     = "#4ade80"
    ERROR       = "#f87171"
    WARNING     = "#fbbf24"
    BORDER      = "#2a3250"

    DEFAULT_STEAMCMD = ""
    DEFAULT_TARGET   = ""
    DEFAULT_APPID    = ""
    SETTINGS_FILE    = os.path.join(os.path.dirname(os.path.abspath(__file__)), "settings.json")

    # ── Oyun Kütüphanesi ─────────────────────────────────────────────────────
    GAMES = [
        {"name": "RimWorld",              "id": "294100",  "emoji": "🪐"},
        {"name": "Cities: Skylines",      "id": "255710",  "emoji": "🏙️"},
        {"name": "Garry's Mod",           "id": "4000",    "emoji": "🔧"},
        {"name": "Stardew Valley",        "id": "413150",  "emoji": "🌾"},
        {"name": "Skyrim SE",             "id": "489830",  "emoji": "⚔️"},
        {"name": "Fallout 4",             "id": "377160",  "emoji": "☢️"},
        {"name": "Kenshi",                "id": "233860",  "emoji": "🏜️"},
        {"name": "Don't Starve Together", "id": "322330",  "emoji": "🕯️"},
        {"name": "Bannerlord",            "id": "261550",  "emoji": "🗡️"},
        {"name": "Hearts of Iron IV",     "id": "394360",  "emoji": "🌍"},
        {"name": "Europa Universalis IV", "id": "236850",  "emoji": "👑"},
        {"name": "Crusader Kings III",    "id": "1158310", "emoji": "⚜️"},
        {"name": "Stellaris",             "id": "281990",  "emoji": "🌌"},
        {"name": "7 Days to Die",         "id": "251570",  "emoji": "🧟"},
        {"name": "Terraria",              "id": "105600",  "emoji": "⛏️"},
    ]

    def __init__(self, root: tk.Tk):
        self.root = root
        self._downloading = False
        self._closing = False
        self._drag_x = 0
        self._drag_y = 0
        self._setup_window()
        self._build_ui()
        self._load_settings()
        self._apply_acrylic()

    def _load_settings(self):
        try:
            with open(self.SETTINGS_FILE, "r", encoding="utf-8") as f:
                s = json.load(f)
            self.app_id_var.set(s.get("app_id", self.DEFAULT_APPID))
            self.col_id_var.set(s.get("col_id", ""))
            self.target_var.set(s.get("target", self.DEFAULT_TARGET))
            self.steamcmd_var.set(s.get("steamcmd", self.DEFAULT_STEAMCMD))
            self.username_var.set(s.get("username", ""))
            self.single_mod_var.set(s.get("single_mode", False))
            self.single_id_var.set(s.get("single_id", ""))
        except Exception:
            pass
        self._toggle_single_mode()

    def _save_settings(self):
        s = {
            "app_id": self.app_id_var.get(),
            "col_id": self.col_id_var.get(),
            "target": self.target_var.get(),
            "steamcmd": self.steamcmd_var.get(),
            "username": self.username_var.get(),
            "single_mode": self.single_mod_var.get(),
            "single_id": self.single_id_var.get(),
        }
        try:
            with open(self.SETTINGS_FILE, "w", encoding="utf-8") as f:
                json.dump(s, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def _toggle_single_mode(self):
        if self.single_mod_var.get():
            self.col_id_frame.pack_forget()
            self.single_id_frame.pack(fill="x", pady=5)
        else:
            self.single_id_frame.pack_forget()
            self.col_id_frame.pack(fill="x", pady=5)

    def _reset_settings(self):
        if self._downloading:
            return
        self.app_id_var.set("")
        self.col_id_var.set("")
        self.target_var.set("")
        self.steamcmd_var.set("")
        self.username_var.set("")
        self.password_var.set("")
        self.guard_var.set("")
        self.single_mod_var.set(False)
        self.single_id_var.set("")
        self._toggle_single_mode()
        
        if os.path.exists(self.SETTINGS_FILE):
            try:
                os.remove(self.SETTINGS_FILE)
            except Exception:
                pass
        
        if self._selected_game_btn and self._selected_game_btn.winfo_exists():
            self._selected_game_btn.config(
                bg=self.SURFACE2, fg=self.TEXT_DIM,
                highlightbackground=self.BORDER)
            self._selected_game_btn = None
            
        self._clear_log()
        self._log("Ayarlar sıfırlandı. Yeni bir indirme için bilgileri doldurun.", "info")

    # ── Window setup ────────────────────────────────────────────────────────
    def _setup_window(self):
        self.root.title("Steam Workshop Downloader")
        self.root.geometry("940x820")
        self.root.resizable(False, False)
        self.root.overrideredirect(True)           # frameless
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self.root.bind("<Map>", self._restore_frame)
        self.root.configure(bg=self.BG)
        self.root.attributes("-alpha", 0.96)
        self._center()

    def _center(self):
        self.root.update_idletasks()
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        w, h = min(940, sw - 40), min(820, sh - 80)
        self.root.geometry(f"{w}x{h}+{(sw-w)//2}+{(sh-h)//2}")

    def _close(self):
        self._closing = True
        if getattr(self, "_downloading", False):
            self._stop_event.set()
        worker = getattr(self, "_worker_thread", None)
        if worker is not None and worker.is_alive():
            self.root.after(100, self._close)
            return
        self.root.destroy()

    def _minimize(self):
        self.root.overrideredirect(False)
        self.root.iconify()

    def _restore_frame(self, event):
        if event.widget is self.root and self.root.state() == "normal":
            self.root.overrideredirect(True)

    def _apply_acrylic(self):
        self.root.update()
        hwnd = ctypes.windll.user32.GetParent(self.root.winfo_id())

        # ── Taskbar'da görünürlük: WS_EX_APPWINDOW ekle, WS_EX_TOOLWINDOW kaldır ──
        GWL_EXSTYLE      = -20
        WS_EX_APPWINDOW  = 0x00040000
        WS_EX_TOOLWINDOW = 0x00000080
        user32 = ctypes.windll.user32
        style  = user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
        style  = (style & ~WS_EX_TOOLWINDOW) | WS_EX_APPWINDOW
        user32.SetWindowLongW(hwnd, GWL_EXSTYLE, style)

        # Acrylic blur
        set_acrylic(hwnd, 0xCC140F0D)

        # İkon ata (PNG, tkinter 3.8+ destekler)
        icon_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "steam_icon.png")
        if os.path.exists(icon_path):
            try:
                self._icon = tk.PhotoImage(file=icon_path)
                self.root.iconphoto(True, self._icon)
            except Exception:
                pass

        # Stil değişikliğinin taskbar'a yansıması için kısa withdraw/deiconify
        self.root.withdraw()
        self.root.after(50, self.root.deiconify)

    # ── Drag support ────────────────────────────────────────────────────────
    def _start_drag(self, e):
        self._drag_x = e.x
        self._drag_y = e.y

    def _on_drag(self, e):
        x = self.root.winfo_x() + e.x - self._drag_x
        y = self.root.winfo_y() + e.y - self._drag_y
        self.root.geometry(f"+{x}+{y}")

    # ── UI Build ─────────────────────────────────────────────────────────────
    def _build_ui(self):
        self._make_titlebar()
        self._make_body()

    def _make_titlebar(self):
        bar = tk.Frame(self.root, bg="#0a0c12", height=42)
        bar.pack(fill="x")
        bar.pack_propagate(False)
        bar.bind("<ButtonPress-1>", self._start_drag)
        bar.bind("<B1-Motion>", self._on_drag)

        # Steam icon (unicode)
        icon = tk.Label(bar, text="⬡", fg=self.ACCENT, bg="#0a0c12",
                        font=("Segoe UI", 16))
        icon.pack(side="left", padx=(14, 6), pady=6)

        title = tk.Label(bar, text="Steam Workshop Downloader",
                         fg=self.TEXT, bg="#0a0c12",
                         font=("Segoe UI", 11, "bold"))
        title.pack(side="left", pady=6)
        title.bind("<ButtonPress-1>", self._start_drag)
        title.bind("<B1-Motion>", self._on_drag)

        # Close / minimise buttons
        for txt, cmd, clr in [("✕", self._close, "#f87171"),
                               ("─", self._minimize, "#fbbf24")]:
            btn = tk.Label(bar, text=txt, fg=self.TEXT_DIM, bg="#0a0c12",
                           font=("Segoe UI", 11), cursor="hand2",
                           width=3, padx=4)
            btn.pack(side="right")
            btn.bind("<Enter>", lambda e, b=btn, c=clr: b.config(fg=c))
            btn.bind("<Leave>", lambda e, b=btn: b.config(fg=self.TEXT_DIM))
            btn.bind("<Button-1>", lambda e, c=cmd: c())

        # Separator
        sep = tk.Frame(self.root, bg=self.BORDER, height=1)
        sep.pack(fill="x")

    def _make_body(self):
        body = tk.Frame(self.root, bg=self.BG)
        body.pack(fill="both", expand=True, padx=22, pady=18)

        # ── Left column: scrollable inputs ───────────────────────────────
        left_outer = tk.Frame(body, bg=self.BG, width=440)
        left_outer.pack(side="left", fill="y", padx=(0, 18))
        left_outer.pack_propagate(False)

        left_canvas = tk.Canvas(left_outer, bg=self.BG, bd=0,
                                highlightthickness=0, width=430)
        left_canvas.pack(side="left", fill="both", expand=True)

        left = tk.Frame(left_canvas, bg=self.BG)
        left_window = left_canvas.create_window((0, 0), window=left, anchor="nw")
        left_canvas.bind("<Configure>", lambda e: left_canvas.itemconfigure(left_window, width=e.width))

        def _on_left_configure(e):
            left_canvas.configure(scrollregion=left_canvas.bbox("all"))
        left.bind("<Configure>", _on_left_configure)

        def _on_mousewheel(e):
            widget = e.widget
            while widget is not None:
                if widget is left_outer:
                    left_canvas.yview_scroll(int(-e.delta / 120), "units")
                    return "break"
                widget = getattr(widget, "master", None)
        self.root.bind("<MouseWheel>", _on_mousewheel, add="+")

        # ── Game picker ──────────────────────────────────────────────────
        self._section_label(left, "🎮  Oyun Seç")

        game_grid = tk.Frame(left, bg=self.BG)
        game_grid.pack(fill="x", pady=(0, 10))

        self.app_id_var   = tk.StringVar(value=self.DEFAULT_APPID)
        self.col_id_var   = tk.StringVar()
        self.target_var   = tk.StringVar(value=self.DEFAULT_TARGET)
        self.steamcmd_var = tk.StringVar(value=self.DEFAULT_STEAMCMD)
        self._selected_game_btn = None

        cols = 3
        for idx, game in enumerate(self.GAMES):
            row, col = divmod(idx, cols)
            btn = tk.Label(
                game_grid,
                text=f"{game['emoji']} {game['name']}",
                bg=self.SURFACE2, fg=self.TEXT_DIM,
                font=("Segoe UI", 8),
                cursor="hand2", padx=6, pady=5,
                relief="flat",
                highlightthickness=1,
                highlightbackground=self.BORDER,
            )
            btn.grid(row=row, column=col, padx=3, pady=3, sticky="ew")
            game_grid.columnconfigure(col, weight=1)

            def _pick(e, g=game, b=btn):
                # Önceki seçimi sıfırla
                if self._selected_game_btn and self._selected_game_btn.winfo_exists():
                    self._selected_game_btn.config(
                        bg=self.SURFACE2, fg=self.TEXT_DIM,
                        highlightbackground=self.BORDER)
                # Yeni seçim — sadece App ID doldur
                b.config(bg=self.ACCENT, fg="#ffffff",
                         highlightbackground=self.ACCENT)
                self._selected_game_btn = b
                self.app_id_var.set(g["id"])

            btn.bind("<Button-1>", _pick)
            btn.bind("<Enter>", lambda e, b=btn: b.config(fg=self.TEXT)
                     if b is not self._selected_game_btn else None)
            btn.bind("<Leave>", lambda e, b=btn: b.config(fg=self.TEXT_DIM)
                     if b is not self._selected_game_btn else None)

        # Ayırıcı çizgi
        tk.Frame(left, bg=self.BORDER, height=1).pack(fill="x", pady=(4, 10))

        # ── Settings fields ──────────────────────────────────────────────
        settings_header = tk.Frame(left, bg=self.BG)
        settings_header.pack(fill="x", pady=(0, 8))
        
        tk.Label(settings_header, text="⚙  Ayarlar", fg=self.ACCENT,
                 bg=self.BG, font=("Segoe UI", 9, "bold")).pack(side="left")
                 
        reset_settings_btn = tk.Label(settings_header, text="🔄 Sıfırla",
                                      fg=self.TEXT_DIM, bg=self.BG,
                                      font=("Segoe UI", 8), cursor="hand2")
        reset_settings_btn.pack(side="right")
        reset_settings_btn.bind("<Button-1>", lambda e: self._reset_settings())
        reset_settings_btn.bind("<Enter>", lambda e: reset_settings_btn.config(fg=self.ERROR))
        reset_settings_btn.bind("<Leave>", lambda e: reset_settings_btn.config(fg=self.TEXT_DIM))

        self.app_id_frame = self._labeled_entry(left, "App ID", self.app_id_var, "Örn: 294100 (RimWorld)")

        # ── Tek Mod ID (checkbox + alan) ───────────────────────────────
        self.single_mod_var = tk.BooleanVar(value=False)
        self.single_id_var  = tk.StringVar()

        chk_frame = tk.Frame(left, bg=self.BG)
        chk_frame.pack(fill="x", pady=(4, 0))
        chk = tk.Checkbutton(
            chk_frame, text="Tek Mod ID ile İndir",
            variable=self.single_mod_var,
            bg=self.BG, fg=self.ACCENT2, activebackground=self.BG,
            activeforeground=self.ACCENT, selectcolor=self.SURFACE2,
            font=("Segoe UI", 8), cursor="hand2",
            command=self._toggle_single_mode
        )
        chk.pack(anchor="w")

        # Container for the conditional ID fields
        self.id_container = tk.Frame(left, bg=self.BG)
        self.id_container.pack(fill="x")

        self.col_id_frame = self._labeled_entry(self.id_container, "Koleksiyon ID", self.col_id_var, "Steam Koleksiyon ID'si")
        self.single_id_frame = self._labeled_entry(self.id_container, "Workshop Mod ID", self.single_id_var, "Örn: 2009463077")

        # Hedef Klasör — klasör seçici
        self._folder_entry(left, "Hedef Klasör  (klasör seçin)", self.target_var)

        # SteamCMD — exe seçici
        self._file_entry(left, "SteamCMD Yolu  (steamcmd.exe seçin)", self.steamcmd_var)

        # ── Giriş (İsteğe Bağlı) ─────────────────────────────────────────
        tk.Frame(left, bg=self.BORDER, height=1).pack(fill="x", pady=(8, 4))
        self._section_label(left, "🔑  Giriş (İsteğe Bağlı — boş = anonim)")

        self.username_var   = tk.StringVar()
        self.password_var   = tk.StringVar()
        self.guard_var      = tk.StringVar()

        self._labeled_entry(left, "Steam Kullanıcı Adı", self.username_var, "boş bırakırsan anonim")
        self._labeled_entry(left, "Şifre", self.password_var, "••••••••", show="*")
        self._labeled_entry(left, "Steam Guard Kodu (varsa)", self.guard_var, "örn: A1B2C")

        tk.Frame(left, bg=self.BG, height=8).pack()

        # Download + Stop butonları yan yana
        btn_row = tk.Frame(left, bg=self.BG)
        btn_row.pack(fill="x", pady=(0, 6))

        self.dl_btn = self._fancy_button(btn_row, "▶  İndirmeyi Başlat", self._start_download)
        self.dl_btn.pack(side="left", fill="x", expand=True, padx=(0, 4))

        self.stop_btn = tk.Label(
            btn_row, text="■  Durdur",
            bg="#3a1a1a", fg="#f87171",
            font=("Segoe UI", 10, "bold"),
            cursor="hand2", pady=10, relief="flat",
        )
        self.stop_btn.pack(side="right", padx=(0, 0))
        self.stop_btn.bind("<Button-1>", lambda e: self._stop_download())
        self.stop_btn.bind("<Enter>", lambda e: self.stop_btn.config(bg="#7f1d1d"))
        self.stop_btn.bind("<Leave>", lambda e: self.stop_btn.config(bg="#3a1a1a"))

        # Status bar
        self.status_var = tk.StringVar(value="Hazır.")
        status = tk.Label(left, textvariable=self.status_var,
                          fg=self.TEXT_DIM, bg=self.BG,
                          font=("Segoe UI", 8), wraplength=310, justify="left")
        status.pack(fill="x")

        # Progress bar
        style = ttk.Style()
        style.theme_use("clam")
        style.configure("Steam.Horizontal.TProgressbar",
                        troughcolor=self.SURFACE,
                        background=self.ACCENT,
                        borderwidth=0, thickness=6)
        self.progress = ttk.Progressbar(left, style="Steam.Horizontal.TProgressbar",
                                        mode="determinate", length=310)
        self.progress.pack(fill="x", pady=(10, 0))

        # ── Right column: log ────────────────────────────────────────────
        right = tk.Frame(body, bg=self.BG)
        right.pack(side="right", fill="both", expand=True)
        log_header = tk.Frame(right, bg=self.BG)
        log_header.pack(fill="x", pady=(0, 8))

        tk.Label(log_header, text="📋  Log", fg=self.ACCENT,
                 bg=self.BG, font=("Segoe UI", 9, "bold")).pack(side="left")

        # Mod sayacı
        self.counter_var = tk.StringVar(value="")
        tk.Label(log_header, textvariable=self.counter_var,
                 fg=self.SUCCESS, bg=self.BG,
                 font=("Segoe UI", 8, "bold")).pack(side="left", padx=(12, 0))

        # Log temizle butonu
        clear_btn = tk.Label(log_header, text="🗑️ Temizle",
                             fg=self.TEXT_DIM, bg=self.BG,
                             font=("Segoe UI", 8), cursor="hand2")
        clear_btn.pack(side="right")
        clear_btn.bind("<Button-1>", lambda e: self._clear_log())
        clear_btn.bind("<Enter>", lambda e: clear_btn.config(fg=self.ERROR))
        clear_btn.bind("<Leave>", lambda e: clear_btn.config(fg=self.TEXT_DIM))

        log_frame = tk.Frame(right, bg=self.SURFACE, bd=0,
                             highlightthickness=1,
                             highlightbackground=self.BORDER)
        log_frame.pack(fill="both", expand=True)

        self.log_text = tk.Text(
            log_frame, bg=self.SURFACE, fg=self.TEXT,
            font=("Cascadia Code", 9), bd=0, padx=10, pady=10,
            wrap="word", state="disabled", cursor="arrow",
            insertbackground=self.ACCENT,
            selectbackground=self.ACCENT,
        )
        self.log_text.pack(side="left", fill="both", expand=True)

        scrollbar = tk.Scrollbar(log_frame, bg=self.SURFACE2,
                                 troughcolor=self.SURFACE,
                                 activebackground=self.ACCENT,
                                 command=self.log_text.yview)
        scrollbar.pack(side="right", fill="y")
        self.log_text.config(yscrollcommand=scrollbar.set)

        # Tag colours
        self.log_text.tag_config("info",    foreground=self.ACCENT2)
        self.log_text.tag_config("success", foreground=self.SUCCESS)
        self.log_text.tag_config("error",   foreground=self.ERROR)
        self.log_text.tag_config("warning", foreground=self.WARNING)
        self.log_text.tag_config("dim",     foreground=self.TEXT_DIM)

        self._log("Steam Workshop Downloader hazır.", "success")
        if self.DEFAULT_STEAMCMD:
            self._log("SteamCMD yolu otomatik ayarlandı:", "dim")
            self._log(f"  {self.DEFAULT_STEAMCMD}", "info")
        else:
            self._log("Lütfen Ayarlar kısmından steamcmd.exe yolunu seçin.", "warning")

    # ── UI Helpers ───────────────────────────────────────────────────────────
    def _section_label(self, parent, text):
        lbl = tk.Label(parent, text=text, fg=self.ACCENT,
                       bg=self.BG, font=("Segoe UI", 9, "bold"))
        lbl.pack(anchor="w", pady=(0, 8))

    def _labeled_entry(self, parent, label, variable, hint, show=""):
        frame = tk.Frame(parent, bg=self.BG)
        frame.pack(fill="x", pady=5)

        tk.Label(frame, text=label, fg=self.TEXT_DIM, bg=self.BG,
                 font=("Segoe UI", 8)).pack(anchor="w")

        entry_opts = dict(
            textvariable=variable,
            bg=self.SURFACE2, fg=self.TEXT,
            insertbackground=self.ACCENT,
            relief="flat", bd=0,
            font=("Segoe UI", 9),
            highlightthickness=1,
            highlightbackground=self.BORDER,
            highlightcolor=self.ACCENT,
            width=38,
        )
        if show:
            entry_opts["show"] = show

        entry = tk.Entry(frame, **entry_opts)
        entry.pack(fill="x", ipady=6)

        def _update_hint(e=None):
            entry.config(fg=self.TEXT if variable.get() else self.TEXT_DIM)

        entry.bind("<FocusIn>",  _update_hint)
        entry.bind("<FocusOut>", _update_hint)

        placeholder = tk.Label(entry, text=hint, fg=self.TEXT_DIM,
                               bg=self.SURFACE2, font=("Segoe UI", 9))

        def refresh_hint(*_):
            if variable.get():
                placeholder.place_forget()
            else:
                placeholder.place(x=6, rely=0.5, anchor="w")

        variable.trace_add("write", refresh_hint)
        placeholder.bind("<Button-1>", lambda e: entry.focus_set())
        refresh_hint()

        return frame

    def _folder_entry(self, parent, label, variable):
        """Klasör seçici: metin kutusu + sağında 📁 butonu."""
        frame = tk.Frame(parent, bg=self.BG)
        frame.pack(fill="x", pady=5)

        tk.Label(frame, text=label, fg=self.TEXT_DIM, bg=self.BG,
                 font=("Segoe UI", 8)).pack(anchor="w")

        row = tk.Frame(frame, bg=self.BG)
        row.pack(fill="x")

        entry = tk.Entry(
            row, textvariable=variable,
            bg=self.SURFACE2, fg=self.TEXT,
            insertbackground=self.ACCENT,
            relief="flat", bd=0,
            font=("Segoe UI", 9),
            highlightthickness=1,
            highlightbackground=self.BORDER,
            highlightcolor=self.ACCENT,
        )
        entry.pack(side="left", fill="x", expand=True, ipady=6)

        def _browse():
            chosen = filedialog.askdirectory(
                title="Hedef klasörü seçin",
                initialdir=variable.get() or "C:/",
            )
            if chosen:  # kullanıcı iptal etmediyse
                variable.set(chosen.replace("/", "\\"))

        browse_btn = tk.Label(
            row, text="📁",
            bg=self.SURFACE2, fg=self.ACCENT,
            font=("Segoe UI", 11),
            cursor="hand2", padx=8, pady=4,
            highlightthickness=1,
            highlightbackground=self.BORDER,
        )
        browse_btn.pack(side="right", padx=(4, 0))
        browse_btn.bind("<Button-1>", lambda e: _browse())
        browse_btn.bind("<Enter>", lambda e: browse_btn.config(bg=self.ACCENT, fg="#ffffff"))
        browse_btn.bind("<Leave>", lambda e: browse_btn.config(bg=self.SURFACE2, fg=self.ACCENT))

    def _file_entry(self, parent, label, variable):
        """Dosya seçici: metin kutusu + 📂 butonu (.exe için)."""
        frame = tk.Frame(parent, bg=self.BG)
        frame.pack(fill="x", pady=5)

        tk.Label(frame, text=label, fg=self.TEXT_DIM, bg=self.BG,
                 font=("Segoe UI", 8)).pack(anchor="w")

        row = tk.Frame(frame, bg=self.BG)
        row.pack(fill="x")

        entry = tk.Entry(
            row, textvariable=variable,
            bg=self.SURFACE2, fg=self.TEXT,
            insertbackground=self.ACCENT,
            relief="flat", bd=0,
            font=("Segoe UI", 9),
            highlightthickness=1,
            highlightbackground=self.BORDER,
            highlightcolor=self.ACCENT,
        )
        entry.pack(side="left", fill="x", expand=True, ipady=6)

        def _browse():
            cur = variable.get()
            init_dir = os.path.dirname(cur) if cur else "C:/"
            chosen = filedialog.askopenfilename(
                title="steamcmd.exe dosyasını seçin",
                initialdir=init_dir,
                filetypes=[("steamcmd.exe", "steamcmd.exe"), ("EXE dosyaları", "*.exe")],
            )
            if chosen:
                variable.set(chosen.replace("/", "\\"))

        browse_btn = tk.Label(
            row, text="📂",
            bg=self.SURFACE2, fg=self.ACCENT,
            font=("Segoe UI", 11),
            cursor="hand2", padx=8, pady=4,
            highlightthickness=1,
            highlightbackground=self.BORDER,
        )
        browse_btn.pack(side="right", padx=(4, 0))
        browse_btn.bind("<Button-1>", lambda e: _browse())
        browse_btn.bind("<Enter>", lambda e: browse_btn.config(bg=self.ACCENT, fg="#ffffff"))
        browse_btn.bind("<Leave>", lambda e: browse_btn.config(bg=self.SURFACE2, fg=self.ACCENT))

    def _fancy_button(self, parent, text, cmd):
        btn = tk.Label(
            parent, text=text,
            bg=self.ACCENT, fg="#ffffff",
            font=("Segoe UI", 10, "bold"),
            cursor="hand2", pady=10,
            relief="flat",
        )
        def _enter(e): btn.config(bg=self.ACCENT2, fg=self.BG)
        def _leave(e): btn.config(bg=self.ACCENT, fg="#ffffff")
        btn.bind("<Enter>", _enter)
        btn.bind("<Leave>", _leave)
        btn.bind("<Button-1>", lambda e: cmd())
        return btn

    # ── Logging ──────────────────────────────────────────────────────────────
    def _log(self, message, tag="info"):
        self.log_text.config(state="normal")
        self.log_text.insert("end", message + "\n", tag)
        line_count = int(self.log_text.index("end-1c").split(".")[0])
        if line_count > 2000:
            self.log_text.delete("1.0", f"{line_count - 2000 + 1}.0")
        self.log_text.see("end")
        self.log_text.config(state="disabled")

    def _clear_log(self):
        self.log_text.config(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.config(state="disabled")
        self.counter_var.set("")

    def _set_status(self, msg):
        self.status_var.set(msg)

    def _set_progress(self, val):
        self.progress["value"] = val

    # ── Download Logic ───────────────────────────────────────────────────────
    def _start_download(self):
        if getattr(self, "_downloading", False) or getattr(self, "_closing", False):
            return
        app_id   = self.app_id_var.get().strip()
        col_id   = self.col_id_var.get().strip()
        target   = self.target_var.get().strip()
        steamcmd = self.steamcmd_var.get().strip()
        username = self.username_var.get().strip()
        password = self.password_var.get()
        guard    = self.guard_var.get().strip()
        single   = self.single_mod_var.get()
        single_id = self.single_id_var.get().strip()

        if single:
            if not all([app_id, single_id, target, steamcmd]):
                messagebox.showwarning("Eksik Bilgi", "App ID, Mod ID, hedef klasör ve SteamCMD gerekli.")
                return
        else:
            if not all([app_id, col_id, target, steamcmd]):
                messagebox.showwarning("Eksik Bilgi", "Lütfen tüm alanları doldurun.")
                return

        if os.path.isdir(steamcmd):
            steamcmd = os.path.join(steamcmd, "steamcmd.exe")
        elif not steamcmd.lower().endswith(".exe"):
            steamcmd += ".exe"

        selected_id = single_id if single else col_id
        if any(not value.isascii() or not value.isdigit() or int(value) <= 0
               for value in (app_id, selected_id)):
            messagebox.showwarning("Hatalı ID", "App ID ve Workshop ID pozitif sayı olmalı.")
            return
        steamcmd = os.path.abspath(steamcmd)
        target = os.path.abspath(target)
        if not os.path.isfile(steamcmd):
            messagebox.showwarning("SteamCMD bulunamadı", steamcmd)
            return
        self._save_settings()
        self._downloading = True
        self._ui_queue = queue.Queue()

        self._log_queue = queue.Queue()
        self._stop_event = threading.Event()
        self._dl_count = 0
        self._dl_total = 0

        self.dl_btn.config(text="⏳  İndiriliyor…", bg="#2a3a5e", cursor="watch")
        self._set_progress(0)
        self._log("─" * 45, "dim")
        if single:
            self._log(f"[→] Tek mod: {single_id}  |  App ID: {app_id}", "info")
        else:
            self._log(f"[→] App ID: {app_id}  |  Koleksiyon: {col_id}", "info")
        self._poll_log()

        def _log_q(msg, tag="info"):
            self._log_queue.put((msg, tag))
            # Sayaç güncellemesi (başarılı indirme satırlarında)
            if "Success. Downloaded" in str(msg):
                self._dl_count += 1
                count, total = self._dl_count, self._dl_total
                self._ui_queue.put(lambda: self.counter_var.set(
                    f"{count}/{total} mod indirildi"))

        def _worker():
            if single:
                items = [single_id]
                _log_q("Mod bilgileri alınıyor…", "dim")
            else:
                _log_q("Koleksiyon bilgileri alınıyor…", "dim")
                items, err = get_collection_items(col_id)
                if err:
                    _log_q(f"[-] {err}", "error")
                    self._ui_queue.put(lambda: self._finish(False))
                    return
                _log_q(f"[+] {len(items)} mod bulundu, başlıklar yükleniyor…", "success")

            # Fetch mod names
            titles_map = get_items_details(items, log_callback=_log_q)

            if single:
                title = titles_map.get(str(single_id), "Bilinmeyen Mod")
                _log_q(f"[+] Mod: {title} ({single_id})", "success")
            else:
                for idx, mid in enumerate(items, 1):
                    title = titles_map.get(str(mid), "Bilinmeyen Mod")
                    _log_q(f"  {idx:3}. {title} ({mid})", "dim")
                _log_q("─" * 40, "dim")

            self._dl_total = len(items)
            self._ui_queue.put(lambda: self.counter_var.set(f"0/{self._dl_total} mod indirildi"))
            self._ui_queue.put(lambda: self._set_status(f"{len(items)} mod indiriliyor…"))

            success = download_mods(
                app_id, items, target, steamcmd,
                log_callback=_log_q,
                progress_callback=lambda v: self._ui_queue.put(lambda v=v: self._set_progress(v)),
                stop_event=self._stop_event,
                username=username, password=password, steam_guard=guard,
                titles_map=titles_map,
            )
            stopped = self._stop_event.is_set()
            self._ui_queue.put(lambda: self._finish(success and not stopped, stopped))

        def _safe_worker():
            try:
                _worker()
            except Exception as exc:
                _log_q(f"[-] Beklenmeyen hata: {exc}", "error")
                self._ui_queue.put(lambda: self._finish(False))

        self._worker_thread = threading.Thread(target=_safe_worker, daemon=True)
        self._worker_thread.start()

    def _poll_log(self):
        """Process bounded batches without starving UI events."""
        try:
            for _ in range(50):
                msg, tag = self._log_queue.get_nowait()
                self._log(msg, tag)
        except (queue.Empty, AttributeError):
            pass
        self._poll_id = self.root.after(40, self._poll_log)
        try:
            for _ in range(100):
                action = self._ui_queue.get_nowait()
                action()
                if not self._downloading:
                    break
        except (queue.Empty, AttributeError):
            pass

    def _stop_download(self):
        if not getattr(self, "_downloading", False):
            return
        if hasattr(self, "_stop_event"):
            self._stop_event.set()
        self.stop_btn.config(text="⏹ Durduruluyor…", bg="#7f1d1d")

    def _finish(self, success, stopped=False):
        self._downloading = False
        # Kuyrukta kalan son mesajları boşalt
        if hasattr(self, "_poll_id"):
            self.root.after_cancel(self._poll_id)
        try:
            while True:
                msg, tag = self._log_queue.get_nowait()
                self._log(msg, tag)
        except (queue.Empty, AttributeError):
            pass

        if stopped:
            self.dl_btn.config(text="■  Durduruldu", bg="#7f1d1d", cursor="hand2")
            self._set_status("İndirme durduruldu.")
        elif success:
            self.dl_btn.config(text="✓  Tamamlandı", bg="#166534", cursor="hand2")
            self._set_status("İndirme tamamlandı.")
        else:
            self.dl_btn.config(text="✕  Hata oluştu", bg="#7f1d1d", cursor="hand2")
            self._set_status("Hata oluştu. Log'u kontrol edin.")

        self.stop_btn.config(text="■  Durdur", bg="#3a1a1a")
        self.root.after(3000, lambda: self.dl_btn.config(
            text="▶  İndirmeyi Başlat", bg=self.ACCENT, cursor="hand2")
            if not self._downloading else None)


# ─── Entry Point ──────────────────────────────────────────────────────────────
def main():
    # DPI-aware (Windows)
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass

    # Taskbar gruplandırma için benzersiz App ID
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            u"SteamWorkshopDownloader.App.1")
    except Exception:
        pass

    root = tk.Tk()
    app = SteamDownloaderApp(root)
    root.mainloop()


if __name__ == "__main__":
    try:
        main()
    except Exception:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), "error.log"),
                  "a", encoding="utf-8") as error_file:
            traceback.print_exc(file=error_file)
        raise
