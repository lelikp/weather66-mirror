"""Зеркало постов Telegram-канала в ВК (и позже в Макс).

Бот (администратор канала) забирает новые посты через getUpdates, переносит их в ВК
и только после успеха подтверждает прочитанное. Запускается внешним cron
(workflow_dispatch) каждые несколько минут. Тексты постов в лог НЕ пишутся
(репозиторий публичный): только id и счётчики.

Переменные: TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL_ID, VK_TOKEN, VK_GROUP_ID.
"""
import os
import sys
import time
import logging
import requests
from mirror import post_to_vk, post_to_max, check_vk, check_max

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s",
                    datefmt="%Y-%m-%d %H:%M:%S")
log = logging.getLogger("tg_mirror")

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHANNEL = os.getenv("TELEGRAM_CHANNEL_ID", "").strip()
API = f"https://api.telegram.org/bot{TOKEN}/"
ALBUM_SETTLE_SEC = 30        # альбом ждём, пока не «устоится»
PARTIAL = []                 # посты, дошедшие не везде
SKIP_MARKERS = ()            # сюда можно добавить фразы постов, которые зеркалить не нужно


def tg(method, **params):
    r = requests.post(API + method, json=params, timeout=40)
    j = r.json()
    if not j.get("ok"):
        raise RuntimeError(f"Telegram {method}: {j.get('description')}")
    return j["result"]


def is_our_channel(chat):
    if not CHANNEL:
        return False
    if CHANNEL.lstrip("@").lower() == str(chat.get("username", "")).lower():
        return True
    return str(chat.get("id")) == CHANNEL


def to_plain(text, entities):
    """Telegram-разметка -> простой текст. Ссылки text_link превращаем в «текст (url)»;
    смещения у Telegram считаются в UTF-16."""
    text = text or ""
    if not entities:
        return text
    b = text.encode("utf-16-le")
    for e in sorted(entities, key=lambda x: x["offset"], reverse=True):
        if e.get("type") == "text_link" and e.get("url"):
            s, n = e["offset"] * 2, e["length"] * 2
            label = b[s:s + n].decode("utf-16-le")
            new = label if e["url"] in label else f"{label} ({e['url']})"
            b = b[:s] + new.encode("utf-16-le") + b[s + n:]
    return b.decode("utf-16-le")


def download_photo(msg):
    best = max(msg["photo"], key=lambda p: p.get("file_size", 0) or p["width"] * p["height"])
    path = tg("getFile", file_id=best["file_id"])["file_path"]
    r = requests.get(f"https://api.telegram.org/file/bot{TOKEN}/{path}", timeout=60)
    r.raise_for_status()
    return r.content


def post_url(msg):
    u = msg["chat"].get("username")
    return f"https://t.me/{u}/{msg['message_id']}" if u else None


def targets():
    """Куда зеркалим: только настроенные получатели."""
    t = []
    if os.getenv("VK_TOKEN") and os.getenv("VK_GROUP_ID"):
        t.append(("ВК", post_to_vk))
    if os.getenv("MAX_TOKEN") and os.getenv("MAX_CHAT_ID"):
        t.append(("Макс", post_to_max))
    return t


def mirror_unit(msgs):
    """msgs — один пост или все части альбома. Возвращает post_id в ВК или None, если нечего слать."""
    text = ""
    for m in msgs:
        t = to_plain(m.get("text") or m.get("caption"), m.get("entities") or m.get("caption_entities"))
        if t:
            text = t
            break
    if any(mk in text for mk in SKIP_MARKERS):
        log.info(f"пост {msgs[0]['message_id']}: уже зеркалится напрямую — пропуск")
        return None
    images, unsupported = [], False
    for m in msgs:
        if "photo" in m:
            images.append(download_photo(m))
        elif not (m.get("text") and len(msgs) == 1):
            unsupported = True      # видео, файл, опрос и т.п.
    if unsupported:
        link = post_url(msgs[0])
        text = (text + "\n\n" if text else "") + (f"Видео и файлы смотрите в Telegram: {link}" if link
                                                  else "Видео и файлы смотрите в Telegram-канале.")
    if not text and not images:
        log.warning(f"пост {msgs[0]['message_id']}: нечего публиковать")
        return None
    done, failed = [], []
    for name, fn in targets():
        try:
            fn(images, text, log, link=post_url(msgs[0]))
            done.append(name)
        except Exception as e:
            log.error(f"❌ пост {msgs[0]['message_id']} → {name}: {e}")
            failed.append(name)
    if failed and not done:
        raise RuntimeError("не доставлено ни в одну сеть: " + ", ".join(failed))
    if failed:     # часть доставлена: повтор дал бы дубли, поэтому пост считаем обработанным, но job краснеет
        PARTIAL.append(f"пост {msgs[0]['message_id']}: не ушёл в {', '.join(failed)}")
    return ", ".join(done) or None


def build_units(updates):
    """Склеиваем части альбомов (одинаковый media_group_id подряд) в единицы."""
    units, cur_gid = [], None
    for u in updates:
        m = u["channel_post"]
        gid = m.get("media_group_id")
        if gid and units and cur_gid == gid:
            units[-1]["msgs"].append(m)
            units[-1]["last_update"] = u["update_id"]
        else:
            units.append({"msgs": [m], "last_update": u["update_id"], "gid": gid})
        cur_gid = gid
    return units


def check():
    """Проверка настроек без публикации (запуск с check=true)."""
    ok = True
    try:
        me = tg("getMe")
        log.info(f"Telegram: бот @{me.get('username')}")
        wh = tg("getWebhookInfo")
        if wh.get("url"):
            log.error("❌ У бота установлен webhook — getUpdates не заработает. Снимите его: deleteWebhook")
            ok = False
        else:
            log.info("✅ webhook не установлен")
    except Exception as e:
        log.error(f"❌ Telegram: {e}"); ok = False
    if not targets():
        log.error("❌ Не настроен ни ВК, ни Макс"); ok = False
    if os.getenv("VK_TOKEN") and os.getenv("VK_GROUP_ID") and check_vk(log) is False:
        ok = False
    if check_max(log) is False:
        ok = False
    return 0 if ok else 7


def main():
    if os.getenv("CHECK") == "1":
        return check()
    if not TOKEN or not CHANNEL:
        log.error("TELEGRAM_BOT_TOKEN/TELEGRAM_CHANNEL_ID не заданы")
        return 2
    updates = tg("getUpdates", timeout=0, allowed_updates=["channel_post"], limit=100)
    log.info(f"Новых обновлений: {len(updates)}")
    if not updates:
        return 0
    if os.getenv("SKIP_BACKLOG") == "1":      # первый запуск: не пересылать то, что уже накопилось
        while updates:
            tg("getUpdates", offset=updates[-1]["update_id"] + 1, limit=1, timeout=0, allowed_updates=["channel_post"])
            updates = tg("getUpdates", timeout=0, allowed_updates=["channel_post"], limit=100)
        log.info("Накопившиеся обновления подтверждены без пересылки")
        return 0
    mine = [u for u in updates if is_our_channel(u["channel_post"]["chat"])]
    last_seen = updates[-1]["update_id"]
    units = build_units(mine)
    confirmed, rc = None, 0
    for unit in units:
        newest = max(m["date"] for m in unit["msgs"])
        if unit["gid"] and time.time() - newest < ALBUM_SETTLE_SEC:
            log.info("Альбом ещё догружается — отложен до следующего запуска")
            break
        try:
            pid = mirror_unit(unit["msgs"])
            log.info(f"✅ пост {unit['msgs'][0]['message_id']} → " + (pid or "пропущен"))
            confirmed = unit["last_update"]
        except Exception as e:
            log.error(f"❌ пост {unit['msgs'][0]['message_id']}: {e}")
            rc = 7
            break               # остальные подождут: порядок постов сохраняется
    else:
        confirmed = last_seen    # всё обработано, чужие чаты тоже подтверждаем
    if confirmed is not None:
        tg("getUpdates", offset=confirmed + 1, limit=1, timeout=0, allowed_updates=["channel_post"])
    for line in PARTIAL:
        log.error(f"⚠ {line} — продублируйте вручную")
        rc = rc or 7
    return rc


if __name__ == "__main__":
    sys.exit(main())
