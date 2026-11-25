# -*- coding: utf-8 -*-
"""
Бот графіків відключень світла — Любомль (Волиньобленерго).
Запускається GitHub Actions кожні ~10 хв. Стан (підписники, графіки,
надіслані нагадування) зберігається у state.json у репозиторії.
"""
import io
import json
import os
import sys
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from PIL import Image

# ===================== НАЛАШТУВАННЯ =====================
ADDRESSES = [
    {"name": "Завальська 12", "queue": "5.2"},
    {"name": "Наталії Ужвій 5А", "queue": "1.2"},
]
REMIND_BEFORE_MIN = 40      # за скільки хвилин до події слати нагадування
API = "https://api-voe-poweron.inneti.net"
TZ = ZoneInfo("Europe/Kyiv")
STATE_FILE = "state.json"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")
# ========================================================

TOKEN = os.environ.get("BOT_TOKEN", "")
TG = f"https://api.telegram.org/bot{TOKEN}"


# ---------------------- Telegram ----------------------
def tg(method, **params):
    try:
        r = requests.post(f"{TG}/{method}", json=params, timeout=30)
        return r.json()
    except Exception as e:
        print("TG error", method, e)
        return {}


def send(chat_id, text, state):
    res = tg("sendMessage", chat_id=chat_id, text=text, parse_mode="HTML",
             disable_web_page_preview=True)
    if not res.get("ok"):
        desc = str(res.get("description", ""))
        print("send fail", chat_id, desc)
        if "blocked" in desc or "chat not found" in desc or "deactivated" in desc:
            if chat_id in state["subs"]:
                state["subs"].remove(chat_id)


def broadcast(text, state):
    for cid in list(state["subs"]):
        send(cid, text, state)


# ---------------------- Розпізнавання картинки ----------------------
def _load_rgb(data):
    im = Image.open(io.BytesIO(data))
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
        bg.alpha_composite(im)
        im = bg
    return im.convert("RGB")


def _is_cell(p):
    r, g, b = p
    return not (r > 230 and g > 230 and b > 230)


def _is_dark(p):
    return sum(p) < 450


def _runs(row):
    runs, start = [], None
    for x, p in enumerate(row):
        c = _is_cell(p)
        if c and start is None:
            start = x
        elif not c and start is not None:
            runs.append((start, x))
            start = None
    if start is not None:
        runs.append((start, len(row)))
    return runs


def parse_image(data):
    """-> {'1.1': [48 x bool], ...}; True = світла немає (півгодинний слот)."""
    im = _load_rgb(data)
    W, H = im.size
    px = im.load()
    bands, cur = [], []
    for y in range(H):
        row = [px[x, y] for x in range(W)]
        big = [r for r in _runs(row) if r[1] - r[0] > W * 0.02]
        if len(big) >= 26:
            cur.append((y, big))
        elif cur:
            bands.append(cur)
            cur = []
    if cur:
        bands.append(cur)
    if not bands:
        raise ValueError("таблицю не знайдено")
    top = max(len(b) for b in bands)
    bands = [b for b in bands if len(b) > top * 0.6]
    if len(bands) != 24:
        raise ValueError(f"очікував 24 рядки черг, знайшов {len(bands)}")
    result = {f"{q}.{s}": [] for q in range(1, 7) for s in (1, 2)}
    for half in (0, 1):
        for i, band in enumerate(bands[half * 12: half * 12 + 12]):
            y, runs = band[len(band) // 2]
            key = f"{i // 2 + 1}.{i % 2 + 1}"
            for x0, x1 in runs[-24:]:
                result[key].append(_is_dark(px[(x0 + x1) // 2, y]))
    return result


def intervals(slots):
    """[bool]*48 -> [(start_slot, end_slot)], end exclusive."""
    out, start = [], None
    for i, off in enumerate(list(slots) + [False]):
        if off and start is None:
            start = i
        elif not off and start is not None:
            out.append((start, i))
            start = None
    return out


def hm(slot):
    return f"{slot // 2:02d}:{slot % 2 * 30:02d}"


# ---------------------- API Волиньобленерго ----------------------
def api_get(path):
    r = requests.get(API + path, headers={"User-Agent": UA,
                                          "Accept": "application/ld+json"},
                     timeout=30)
    r.raise_for_status()
    return r.json()


def latest_graphs():
    """-> {'2026-11-25': '/media/...png', ...} — найновіша картинка на кожну дату."""
    found = {}
    try:
        data = api_get("/api/archive_gpv_graphs?page=1")
        for m in data.get("hydra:member", []):
            day = (m.get("dateGraph") or "")[:10]
            url = ((m.get("imageUrl") or {}).get("contentUrl"))
            if not day or not url:
                continue
            gid = m.get("id", 0)
            if day not in found or gid > found[day][0]:
                found[day] = (gid, url)
    except Exception as e:
        print("archive error", e)
    graphs = {d: u for d, (_, u) in found.items()}

    # запасний варіант — options (картинка на сьогодні/завтра)
    if not graphs:
        try:
            today = datetime.now(TZ).date()
            data = api_get("/api/options?page=1")
            for m in data.get("hydra:member", []):
                key, val = m.get("optionKey") or m.get("option_key"), m.get("value")
                if not val:
                    continue
                if key == "pw_gpv_image_today":
                    graphs[today.isoformat()] = val
                elif key == "pw_gpv_image_tomorrow":
                    graphs[(today + timedelta(days=1)).isoformat()] = val
        except Exception as e:
            print("options error", e)
    return graphs


def download(url):
    r = requests.get(API + url, headers={"User-Agent": UA}, timeout=60)
    r.raise_for_status()
    return r.content


# ---------------------- Тексти ----------------------
def day_label(day_iso):
    d = datetime.fromisoformat(day_iso).date()
    today = datetime.now(TZ).date()
    tail = {0: " (сьогодні)", 1: " (завтра)"}.get((d - today).days, "")
    return f"{d.strftime('%d.%m')}{tail}"


def schedule_text(day_iso, info, header="📅 Графік на"):
    lines = [f"{header} <b>{day_label(day_iso)}</b>"]
    for a in ADDRESSES:
        slots = info["queues"].get(a["queue"])
        lines.append("")
        lines.append(f"🏠 <b>{a['name']}</b> (черга {a['queue']})")
        if slots is None:
            lines.append("   ❓ не вдалося розпізнати")
            continue
        iv = intervals([c == "1" for c in slots])
        if not iv:
            lines.append("   ✅ відключень немає")
        else:
            for s, e in iv:
                lines.append(f"   🔴 {hm(s)} – {hm(e)}")
    return "\n".join(lines)


def current_text(state):
    today = datetime.now(TZ).date()
    days = sorted(d for d in state["days"] if d >= today.isoformat())
    if not days:
        return "Графіків поки немає 🤷‍♂️ Як тільки з'являться — напишу."
    return "\n\n".join(schedule_text(d, state["days"][d]) for d in days)


# ---------------------- Логіка ----------------------
def handle_updates(state):
    res = tg("getUpdates", offset=state["offset"], timeout=0)
    for u in res.get("result", []):
        state["offset"] = u["update_id"] + 1
        msg = u.get("message") or {}
        cid = (msg.get("chat") or {}).get("id")
        text = (msg.get("text") or "").strip().lower()
        if not cid:
            continue
        if text.startswith("/start"):
            if cid not in state["subs"]:
                state["subs"].append(cid)
            addrs = "\n".join(f"🏠 {a['name']} — черга {a['queue']}" for a in ADDRESSES)
            send(cid, "Привіт! Ти підписаний на сповіщення про відключення світла "
                      "(Любомль):\n" + addrs +
                      f"\n\nНагадаю приблизно за {REMIND_BEFORE_MIN} хв до вимкнення "
                      "і перед тим, як мають дати світло.\n\n"
                      "/graf — актуальний графік\n/stop — відписатись", state)
            send(cid, current_text(state), state)
        elif text.startswith("/stop"):
            if cid in state["subs"]:
                state["subs"].remove(cid)
            send(cid, "Відписав. Захочеш назад — /start", state)
        elif text.startswith("/graf") or text.startswith("/grafik"):
            send(cid, current_text(state), state)
        else:
            send(cid, "Команди:\n/graf — актуальний графік\n/start — підписатись\n"
                      "/stop — відписатись", state)


def update_graphs(state):
    today = datetime.now(TZ).date().isoformat()
    graphs = latest_graphs()
    print("graphs:", graphs)
    for day, url in sorted(graphs.items()):
        if day < today:
            continue
        old = state["days"].get(day)
        if old and old.get("img") == url:
            continue
        try:
            parsed = parse_image(download(url))
        except Exception as e:
            print("parse error", day, url, e)
            if old and old.get("bad_img") == url:
                continue
            state["days"].setdefault(day, {"img": None, "queues": {}})["bad_img"] = url
            broadcast(f"⚠️ Вийшов новий графік на {day_label(day)}, але я не зміг його "
                      f"розпізнати. Глянь картинку:\n{API}{url}", state)
            continue
        queues = {a["queue"]: "".join("1" if x else "0" for x in parsed[a["queue"]])
                  for a in ADDRESSES}
        new = {"img": url, "queues": queues}
        state["days"][day] = new
        if old and old.get("queues") == queues:
            print("new image, same schedule for our queues", day)
            continue
        header = "⚠️ Графік ЗМІНИЛИ на" if old and old.get("queues") else "📅 Графік на"
        broadcast(schedule_text(day, new, header), state)


def events(state):
    """Список подій (час, тип, адреса) з урахуванням переходу через північ."""
    out = []
    for a in ADDRESSES:
        q = a["queue"]
        slots, base = [], None
        for day in sorted(state["days"]):
            info = state["days"][day]
            s = info.get("queues", {}).get(q)
            d0 = datetime.fromisoformat(day).replace(tzinfo=TZ)
            if base is None:
                base = d0
            # заповнюємо пропущені дні «світло є»
            gap = int((d0 - base).total_seconds() // 1800) - len(slots)
            if gap > 0:
                slots += [False] * gap
            slots += [c == "1" for c in s] if s else [False] * 48
        if base is None:
            continue
        for s, e in intervals(slots):
            out.append((base + timedelta(minutes=30 * s), "off",
                        a, base + timedelta(minutes=30 * e)))
            out.append((base + timedelta(minutes=30 * e), "on", a, None))
    return out


def reminders(state):
    now = datetime.now(TZ)
    sent = set(state["sent"])
    for when, kind, a, until in sorted(events(state), key=lambda x: x[0]):
        key = f"{kind}|{a['queue']}|{when.isoformat()}"
        if key in sent:
            continue
        # кінець доби, а графіка на наступний день ще нема — невідомо, чи дадуть світло
        if kind == "on" and when.hour == 0 and when.minute == 0 \
                and when.date().isoformat() not in state["days"]:
            continue
        mins = (when - now).total_seconds() / 60
        if 0 < mins <= REMIND_BEFORE_MIN:
            if kind == "off":
                text = (f"⚠️ <b>{a['name']}</b>: о {when:%H:%M} вимкнуть світло "
                        f"(до {until:%H:%M}). Лишилось ~{int(mins)} хв.")
            else:
                text = (f"💡 <b>{a['name']}</b>: світло мають дати о {when:%H:%M} "
                        f"(~через {int(mins)} хв).")
            broadcast(text, state)
            sent.add(key)
    # чистимо старе
    cutoff = (now - timedelta(days=2)).isoformat()
    state["sent"] = sorted(k for k in sent if k.split("|")[2] >= cutoff)


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            st = json.load(f)
    except Exception:
        st = {}
    st.setdefault("offset", 0)
    st.setdefault("subs", [])
    st.setdefault("days", {})
    st.setdefault("sent", [])
    return st


def save_state(st):
    yesterday = (datetime.now(TZ).date() - timedelta(days=1)).isoformat()
    st["days"] = {d: v for d, v in st["days"].items() if d >= yesterday}
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=1, sort_keys=True)


def main():
    if not TOKEN:
        sys.exit("BOT_TOKEN не задано")
    state = load_state()
    if not state.get("commands_set"):
        tg("setMyCommands", commands=[
            {"command": "graf", "description": "Актуальний графік"},
            {"command": "start", "description": "Підписатись"},
            {"command": "stop", "description": "Відписатись"},
        ])
        state["commands_set"] = True
    update_graphs(state)
    handle_updates(state)
    reminders(state)
    save_state(state)
    print("ok, subs:", len(state["subs"]))


if __name__ == "__main__":
    main()
