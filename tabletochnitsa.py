# -*- coding: utf-8 -*-
"""
Таблеточница для macOS — один файл, Python + tkinter.

Перенос версии для Windows с теми же функциями и тем же форматом data.json:
один и тот же файл данных можно открывать и Tabletochnitsa.exe, и
Tabletochnitsa.app (например, если обе лежат в одной папке на флешке).

Что отличается от Windows-версии:
- data.json лежит рядом с Tabletochnitsa.app; если программа в «Программах»
  (/Applications) или macOS запустила её из карантинной копии, данные
  хранятся в ~/Library/Application Support/Tabletochnitsa;
- кроме окна поверх всех окон приходит системное уведомление macOS
  (его видно и поверх полноэкранных программ) и звук;
- если Mac спал в момент приёма, напоминание придёт сразу после пробуждения
  (в пределах текущих суток), а не потеряется;
- красная кнопка закрытия сворачивает окно в Dock, напоминания продолжают
  работать; выход — ⌘Q.
"""

import csv
import io
import json
import os
import smtplib
import ssl
import subprocess
import sys
import threading
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, time as dtime
from email.message import EmailMessage

APP_TITLE = "Таблеточница"
CHECK_INTERVAL_MS = 10_000      # как часто проверять, не пора ли напомнить
SNOOZE_MINUTES = 10             # «напомнить позже»
LATE_MINUTES = 3                # позже этого напоминание помечается «с опозданием»
IS_MAC = sys.platform == "darwin"


# ======================= Пути =======================

def app_location():
    """Папка, где лежит программа. Для собранного приложения это папка,
    в которой лежит сам Tabletochnitsa.app, а не его внутренности."""
    if getattr(sys, "frozen", False):
        exe = os.path.abspath(sys.executable)
        marker = ".app/Contents/MacOS/"
        i = exe.find(marker)
        if i != -1:
            return os.path.dirname(exe[:i + len(".app")])
        return os.path.dirname(exe)
    return os.path.dirname(os.path.abspath(__file__))


def support_dir():
    if IS_MAC:
        d = os.path.expanduser("~/Library/Application Support/Tabletochnitsa")
    else:
        d = os.path.join(os.path.expanduser("~"), ".tabletochnitsa")
    os.makedirs(d, exist_ok=True)
    return d


def choose_data_dir(location):
    """Рядом с программой (на флешке данные переезжают вместе с ней), кроме
    случаев, когда туда писать нельзя или не стоит."""
    home_apps = os.path.expanduser("~/Applications")
    in_apps = any(location == p or location.startswith(p + "/")
                  for p in ("/Applications", home_apps))
    if "/AppTranslocation/" in location or in_apps or not os.access(location, os.W_OK):
        return support_dir()
    return location


DATA_DIR = choose_data_dir(app_location())
DATA_PATH = os.path.join(DATA_DIR, "data.json")
CONFIG_PATHS = [os.path.join(DATA_DIR, "config.json"),
                os.path.join(app_location(), "config.json")]


# ======================= Время =======================

def today_str(now=None):
    """Локальная дата (не UTC), поэтому ночью день не «съезжает»."""
    return (now or datetime.now()).strftime("%Y-%m-%d")


def normalize_time(text):
    """'8:00' -> '08:00'. Возвращает None, если это не время ЧЧ:ММ."""
    try:
        return datetime.strptime(text.strip(), "%H:%M").strftime("%H:%M")
    except ValueError:
        return None


def parse_times(text):
    """'8:00, 20:00' -> (['08:00', '20:00'], [неверные значения])."""
    raw = [p for p in text.replace(";", ",").replace(" ", ",").split(",") if p.strip()]
    good, bad = [], []
    for p in raw:
        t = normalize_time(p)
        if t is None:
            bad.append(p.strip())
        elif t not in good:
            good.append(t)
    return sorted(good), bad


def slot_datetime(day_date, hhmm):
    h, m = map(int, hhmm.split(":"))
    return datetime.combine(day_date, dtime(h, m))


def newly_due(slots, last, now):
    """Неотмеченные приёмы, время которых наступило в промежутке (last, now].
    Если Mac спал или система притормозила программу, пропущенные за
    сегодня приёмы тоже попадают сюда, а не теряются."""
    return [s for s in slots
            if not s["taken"] and last < slot_datetime(now.date(), s["time"]) <= now]


# ======================= Данные =======================

class Store:
    """Список пунктов и история отметок в одном JSON-файле
    (тот же формат, что у версии для Windows)."""

    def __init__(self, path):
        self.path = path
        self.items = []      # {"id", "name", "note", "times": ["08:00", ...]}
        self.history = {}    # "ГГГГ-ММ-ДД" -> ["<id>|ЧЧ:ММ", ...]
        self.load()

    def load(self):
        if not os.path.exists(self.path):
            return
        try:
            with open(self.path, encoding="utf-8") as f:
                data = json.load(f)
            self.items = data.get("items", [])
            self.history = data.get("history", {})
        except (OSError, ValueError, AttributeError):
            # Файл повреждён — не затираем, а откладываем в сторону.
            try:
                os.replace(self.path, self.path + ".broken")
            except OSError:
                pass
            self.items, self.history = [], {}

    def save(self):
        """Запись через временный файл: если флешку выдернут посреди
        записи, старый data.json останется целым."""
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"items": self.items, "history": self.history},
                      f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.path)

    # --- пункты ---
    def add_item(self, name, note, times):
        item = {"id": uuid.uuid4().hex[:8], "name": name.strip(),
                "note": note.strip(), "times": sorted(times)}
        self.items.append(item)
        return item

    def update_item(self, item_id, name, note, times):
        for it in self.items:
            if it["id"] == item_id:
                it.update(name=name.strip(), note=note.strip(), times=sorted(times))
                return

    def delete_item(self, item_id):
        self.items = [it for it in self.items if it["id"] != item_id]
        for day in self.history:
            self.history[day] = [k for k in self.history[day]
                                 if not k.startswith(item_id + "|")]

    def find(self, item_id):
        return next((it for it in self.items if it["id"] == item_id), None)

    # --- слоты приёма на день ---
    def slots(self, day):
        out = []
        for it in self.items:
            for t in it["times"]:
                key = f"{it['id']}|{t}"
                out.append({"key": key, "item_id": it["id"], "name": it["name"],
                            "note": it.get("note", ""), "time": t,
                            "taken": key in self.history.get(day, [])})
        out.sort(key=lambda s: (s["time"], s["name"].lower()))
        return out

    def set_taken(self, day, key, taken):
        lst = self.history.setdefault(day, [])
        if taken and key not in lst:
            lst.append(key)
        if not taken and key in lst:
            lst.remove(key)

    def reset_day(self, day):
        self.history[day] = []

    # --- импорт ---
    def import_items(self, parsed):
        """Добавляет пункты из CSV; точные дубликаты (имя + время) пропускает."""
        added = skipped = 0
        for p in parsed:
            dup = any(it["name"].lower() == p["name"].lower()
                      and it["times"] == sorted(p["times"]) for it in self.items)
            if dup:
                skipped += 1
            else:
                self.add_item(p["name"], p["note"], p["times"])
                added += 1
        return added, skipped


# ======================= CSV =======================

def decode_bytes(raw):
    """UTF-8 (Excel для Mac: «CSV UTF-8») или Windows-1251 (Excel на русской Windows)."""
    for enc in ("utf-8-sig", "cp1251"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def parse_csv(text):
    """Столбцы: name,note,times (времена через ; ). Разделитель — запятая
    или точка с запятой (Excel с русской локалью сохраняет с ';')."""
    first = text.splitlines()[0] if text.strip() else ""
    delim = ";" if first.count(";") > first.count(",") else ","
    rows = list(csv.reader(io.StringIO(text), delimiter=delim))
    if rows and rows[0] and rows[0][0].strip().lower() in ("name", "название", "имя"):
        rows = rows[1:]
    items, errors = [], []
    for i, row in enumerate(rows, start=1):
        if not row or not row[0].strip():
            continue
        if len(row) < 3:
            errors.append(f"строка {i}: нужно три столбца name,note,times")
            continue
        # Всё, что правее второго столбца, — времена (Excel с «;» разносит
        # «08:00;20:00» по разным столбцам).
        times, bad = parse_times(",".join(row[2:]))
        if bad or not times:
            errors.append(f"строка {i} («{row[0].strip()}»): неверное время {bad or ''}".strip())
            continue
        items.append({"name": row[0].strip(), "note": row[1].strip(), "times": times})
    return items, errors


# ======================= Уведомления наружу (необязательно) =======================

def load_config():
    for path in CONFIG_PATHS:
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            continue
    return {}


def ssl_context():
    """В собранном приложении системные сертификаты Python не видны,
    поэтому берём их из certifi (кладётся в .app при сборке)."""
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


def _send_telegram(cfg, text):
    token, chat_id = cfg.get("token", ""), cfg.get("chat_id", "")
    if not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    body = urllib.parse.urlencode({"chat_id": chat_id, "text": text}).encode()
    urllib.request.urlopen(urllib.request.Request(url, data=body), timeout=15,
                           context=ssl_context()).read()


def _send_email(cfg, subject, text):
    need = ("smtp_host", "user", "password", "to")
    if not all(cfg.get(k) for k in need):
        return
    msg = EmailMessage()
    msg["Subject"], msg["From"], msg["To"] = subject, cfg["user"], cfg["to"]
    msg.set_content(text)
    port = int(cfg.get("smtp_port", 465))
    with smtplib.SMTP_SSL(cfg["smtp_host"], port, timeout=20, context=ssl_context()) as s:
        s.login(cfg["user"], cfg["password"])
        s.send_message(msg)


def notify_external(config, subject, text):
    """Отправка в фоне, чтобы окно не зависало при плохой сети.
    Ошибки молча игнорируются: основное напоминание — окно на экране."""
    def work():
        for fn, args in ((_send_telegram, (config.get("telegram", {}), text)),
                         (_send_email, (config.get("email", {}), subject, text))):
            try:
                fn(*args)
            except Exception:
                pass
    threading.Thread(target=work, daemon=True).start()


def system_notification(title, text):
    """Уведомление в Центре уведомлений macOS. Текст передаётся аргументами,
    поэтому кавычки в названии ничего не ломают."""
    if not IS_MAC:
        return
    try:
        subprocess.Popen(
            ["osascript",
             "-e", "on run argv",
             "-e", "display notification (item 2 of argv) with title (item 1 of argv)",
             "-e", "end run",
             title, text],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def reveal_in_finder(path):
    try:
        if os.path.exists(path):
            subprocess.Popen(["open", "-R", path])
        else:
            subprocess.Popen(["open", os.path.dirname(path)])
    except Exception:
        pass


def reminder_text(slot, late=False):
    text = f"Пора: {slot['name']} ({slot['time']})"
    if late:
        text += " — с опозданием"
    return text + (f"\n{slot['note']}" if slot["note"] else "")


# ======================= Окно =======================

def run_gui():
    import tkinter as tk
    import tkinter.font as tkfont
    from tkinter import ttk, messagebox, filedialog

    store = Store(DATA_PATH)
    config = load_config()

    root = tk.Tk()
    root.title(APP_TITLE)
    root.geometry("480x580")
    root.minsize(400, 440)

    family = tkfont.nametofont("TkDefaultFont").actual("family")
    base = 13 if IS_MAC else 10
    font_head = (family, base + 1, "bold")
    font_popup_time = (family, base + 1)
    font_popup_name = (family, base + 7, "bold")

    now0 = datetime.now()
    state = {
        "day": today_str(now0),
        "last": now0 - timedelta(minutes=1),   # до какого момента уже проверили
        "minute": "",          # для обновления пометок «время прошло» раз в минуту
        "fired": set(),        # "<день>|<ключ>" — уже напомнили
        "snoozed": {},         # "<день>|<ключ>" -> когда напомнить снова
        "popups": {},          # "<день>|<ключ>" -> открытое окно напоминания
    }

    def safe_save():
        try:
            store.save()
            status.set("Сохранено " + datetime.now().strftime("%H:%M:%S"))
        except OSError as e:
            status.set("Не удалось сохранить")
            messagebox.showerror(APP_TITLE, f"Не удалось записать data.json:\n{e}\n\n"
                                            "Проверьте, что флешка вставлена и не защищена от записи.")

    # ---------- вкладка «Сегодня» ----------
    nb = ttk.Notebook(root)
    nb.pack(fill="both", expand=True, padx=8, pady=(8, 0))

    tab_today = ttk.Frame(nb)
    tab_setup = ttk.Frame(nb)
    nb.add(tab_today, text="Сегодня")
    nb.add(tab_setup, text="Список и время")

    head = tk.StringVar()
    ttk.Label(tab_today, textvariable=head, font=font_head).pack(anchor="w", padx=10, pady=(10, 2))
    summary = tk.StringVar()
    ttk.Label(tab_today, textvariable=summary).pack(anchor="w", padx=10, pady=(0, 6))
    list_frame = ttk.Frame(tab_today)
    list_frame.pack(fill="both", expand=True, padx=10)

    def toggle(slot_key, var):
        store.set_taken(state["day"], slot_key, bool(var.get()))
        safe_save()
        refresh_today()

    def refresh_today():
        for w in list_frame.winfo_children():
            w.destroy()
        now = datetime.now()
        head.set(now.strftime("%d.%m.%Y"))
        slots = store.slots(state["day"])
        if not slots:
            ttk.Label(list_frame, text="Список пуст.\nДобавьте пункты во вкладке «Список и время».",
                      justify="left").pack(anchor="w", pady=10)
            summary.set("")
            return
        hm = now.strftime("%H:%M")
        done = sum(1 for s in slots if s["taken"])
        summary.set(f"Принято: {done} из {len(slots)}")
        for s in slots:
            row = ttk.Frame(list_frame)
            row.pack(fill="x", pady=2)
            var = tk.IntVar(value=1 if s["taken"] else 0)
            overdue = (not s["taken"]) and s["time"] < hm
            label = f"{s['time']}  {s['name']}" + (f" — {s['note']}" if s["note"] else "")
            if overdue:
                label += "   (время прошло)"
            ttk.Checkbutton(row, text=label, variable=var,
                            command=lambda k=s["key"], v=var: toggle(k, v)).pack(anchor="w")

    def reset_today():
        if messagebox.askyesno(APP_TITLE, "Сбросить все отметки за сегодня?"):
            store.reset_day(state["day"])
            safe_save()
            refresh_today()

    ttk.Button(tab_today, text="Сбросить отметки за сегодня", command=reset_today).pack(pady=8)

    # ---------- вкладка «Список и время» ----------
    lb = tk.Listbox(tab_setup, activestyle="none", exportselection=False)
    lb.pack(fill="both", expand=True, padx=10, pady=(10, 6))

    def refresh_setup():
        lb.delete(0, "end")
        for it in store.items:
            note = f" ({it['note']})" if it.get("note") else ""
            lb.insert("end", f"{it['name']}{note} — {', '.join(it['times'])}")

    def selected_item():
        sel = lb.curselection()
        return store.items[sel[0]] if sel else None

    def edit_dialog(item=None):
        win = tk.Toplevel(root)
        win.title("Изменить" if item else "Добавить")
        win.transient(root)
        win.resizable(False, False)
        frm = ttk.Frame(win, padding=12)
        frm.pack()
        fields = {}
        first_entry = None
        for r, (key, caption, hint) in enumerate((
                ("name", "Название", ""),
                ("note", "Доза / заметка", ""),
                ("times", "Время", "через запятую: 08:00, 20:00"))):
            ttk.Label(frm, text=caption).grid(row=r * 2, column=0, sticky="w")
            var = tk.StringVar()
            entry = ttk.Entry(frm, textvariable=var, width=30)
            entry.grid(row=r * 2, column=1, padx=(8, 0), pady=2)
            first_entry = first_entry or entry
            if hint:
                ttk.Label(frm, text=hint, foreground="gray").grid(row=r * 2 + 1, column=1, sticky="w", padx=(8, 0))
            fields[key] = var
        if item:
            fields["name"].set(item["name"])
            fields["note"].set(item.get("note", ""))
            fields["times"].set(", ".join(item["times"]))

        def ok(_event=None):
            name = fields["name"].get().strip()
            times, bad = parse_times(fields["times"].get())
            if not name:
                messagebox.showwarning(APP_TITLE, "Введите название.", parent=win)
                return
            if bad or not times:
                messagebox.showwarning(APP_TITLE, "Укажите время в формате ЧЧ:ММ, например 08:00."
                                       + (f"\nНе распознано: {', '.join(bad)}" if bad else ""), parent=win)
                return
            if item:
                store.update_item(item["id"], name, fields["note"].get(), times)
            else:
                store.add_item(name, fields["note"].get(), times)
            safe_save()
            refresh_setup()
            refresh_today()
            win.destroy()

        btns = ttk.Frame(frm)
        btns.grid(row=10, column=0, columnspan=2, pady=(10, 0), sticky="e")
        ttk.Button(btns, text="Отмена", command=win.destroy).pack(side="left", padx=4)
        ttk.Button(btns, text="Сохранить", command=ok, default="active").pack(side="left")
        win.bind("<Return>", ok)
        win.bind("<Escape>", lambda e: win.destroy())
        win.wait_visibility()
        win.grab_set()
        first_entry.focus_set()

    def delete_selected():
        it = selected_item()
        if it and messagebox.askyesno(APP_TITLE, f"Удалить «{it['name']}»?"):
            store.delete_item(it["id"])
            safe_save()
            refresh_setup()
            refresh_today()

    def import_csv():
        path = filedialog.askopenfilename(title="CSV со списком",
                                          filetypes=[("CSV", "*.csv"), ("Все файлы", "*.*")])
        if not path:
            return
        try:
            with open(path, "rb") as f:
                parsed, errors = parse_csv(decode_bytes(f.read()))
        except OSError as e:
            messagebox.showerror(APP_TITLE, f"Не удалось прочитать файл:\n{e}")
            return
        added, skipped = store.import_items(parsed)
        if added:
            safe_save()
        refresh_setup()
        refresh_today()
        msg = f"Добавлено: {added}"
        if skipped:
            msg += f"\nУже были в списке: {skipped}"
        if errors:
            msg += "\n\nПропущены строки с ошибками:\n" + "\n".join(errors[:10])
        messagebox.showinfo(APP_TITLE, msg)

    bar = ttk.Frame(tab_setup)
    bar.pack(fill="x", padx=10, pady=(0, 10))
    ttk.Button(bar, text="Добавить", command=lambda: edit_dialog()).pack(side="left")
    ttk.Button(bar, text="Изменить",
               command=lambda: selected_item() and edit_dialog(selected_item())).pack(side="left", padx=4)
    ttk.Button(bar, text="Удалить", command=delete_selected).pack(side="left")
    ttk.Button(bar, text="Импорт CSV…", command=import_csv).pack(side="right")
    lb.bind("<Double-Button-1>", lambda e: selected_item() and edit_dialog(selected_item()))

    bottom = ttk.Frame(root)
    bottom.pack(fill="x", padx=10, pady=4)
    status = tk.StringVar(value="")
    ttk.Label(bottom, textvariable=status, foreground="gray").pack(side="left")
    if IS_MAC:
        ttk.Button(bottom, text="Где мои данные",
                   command=lambda: reveal_in_finder(DATA_PATH)).pack(side="right")

    # ---------- поведение окна на Mac ----------
    def show_main():
        root.deiconify()
        root.lift()

    def on_close():
        # Красная кнопка не выключает напоминания: окно уходит в Dock.
        if IS_MAC:
            root.iconify()
        else:
            root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    if IS_MAC:
        root.createcommand("::tk::mac::ReopenApplication", show_main)   # щелчок по значку в Dock
        root.bind("<Command-w>", lambda e: on_close())
        root.bind("<Command-n>", lambda e: edit_dialog())

    # ---------- напоминания ----------
    def beep():
        if IS_MAC:
            sound = "/System/Library/Sounds/Glass.aiff"
            if os.path.exists(sound):
                try:
                    subprocess.Popen(["afplay", sound],
                                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                    return
                except Exception:
                    pass
        try:
            import winsound
            winsound.MessageBeep(winsound.MB_ICONEXCLAMATION)
        except Exception:
            root.bell()

    def show_popup(slot, fire_key, late=False):
        old = state["popups"].get(fire_key)
        if old is not None:
            try:
                if old.winfo_exists():
                    old.lift()
                    return
            except tk.TclError:
                pass
        win = tk.Toplevel(root)
        state["popups"][fire_key] = win
        win.title("Пора принять")
        win.attributes("-topmost", True)
        win.resizable(False, False)
        frm = ttk.Frame(win, padding=18)
        frm.pack()
        ttk.Label(frm, text=slot["time"] + ("  (с опозданием)" if late else ""),
                  font=font_popup_time).pack()
        ttk.Label(frm, text=slot["name"], font=font_popup_name).pack(pady=(2, 0))
        if slot["note"]:
            ttk.Label(frm, text=slot["note"]).pack(pady=(2, 0))

        day = fire_key.split("|", 1)[0]

        def taken():
            store.set_taken(day, slot["key"], True)
            safe_save()
            refresh_today()
            win.destroy()

        def later():
            state["snoozed"][fire_key] = datetime.now() + timedelta(minutes=SNOOZE_MINUTES)
            win.destroy()

        row = ttk.Frame(frm)
        row.pack(pady=(14, 0))
        ttk.Button(row, text="Принято", command=taken, default="active").pack(side="left", padx=4)
        ttk.Button(row, text=f"Через {SNOOZE_MINUTES} мин", command=later).pack(side="left", padx=4)
        ttk.Button(row, text="Закрыть", command=win.destroy).pack(side="left", padx=4)
        win.update_idletasks()
        w, h = win.winfo_reqwidth(), win.winfo_reqheight()
        x = (win.winfo_screenwidth() - w) // 2
        y = (win.winfo_screenheight() - h) // 3
        win.geometry(f"+{x}+{y}")
        win.lift()
        win.after(100, win.focus_force)
        beep()

    def tick():
        try:
            now = datetime.now()
            day = today_str(now)
            if day != state["day"]:          # наступили новые сутки
                state["day"] = day
                state["fired"].clear()
                state["snoozed"].clear()
                state["last"] = datetime.combine(now.date(), dtime.min) - timedelta(seconds=1)
                refresh_today()
            if now < state["last"]:          # часы перевели назад
                state["last"] = now - timedelta(minutes=1)

            slots = store.slots(state["day"])
            for s in newly_due(slots, state["last"], now):
                fire_key = f"{state['day']}|{s['key']}"
                if fire_key in state["fired"]:
                    continue
                state["fired"].add(fire_key)
                late = now - slot_datetime(now.date(), s["time"]) > timedelta(minutes=LATE_MINUTES)
                text = reminder_text(s, late)
                notify_external(config, APP_TITLE, text)
                system_notification(APP_TITLE, text)
                show_popup(s, fire_key, late)

            for s in slots:
                if s["taken"]:
                    continue
                fire_key = f"{state['day']}|{s['key']}"
                snooze_until = state["snoozed"].get(fire_key)
                if snooze_until and now >= snooze_until:
                    del state["snoozed"][fire_key]
                    system_notification(APP_TITLE, reminder_text(s))
                    show_popup(s, fire_key)

            state["last"] = now
            minute = now.strftime("%H:%M")
            if minute != state["minute"]:    # раз в минуту обновим пометки «время прошло»
                state["minute"] = minute
                refresh_today()
        finally:
            root.after(CHECK_INTERVAL_MS, tick)

    refresh_setup()
    refresh_today()
    root.after(1000, tick)
    root.mainloop()


if __name__ == "__main__":
    run_gui()
