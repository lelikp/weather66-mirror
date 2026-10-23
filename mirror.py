"""Зеркала публикаций: ВКонтакте (группа). Макс добавим, когда появится канал.

Нужны переменные окружения (GitHub Secrets):
  VK_TOKEN     — ключ доступа сообщества (права: стена, фотографии, управление)
  VK_GROUP_ID  — числовой id сообщества (допустимо и короткое имя, например weather66)
"""
import io
import os
import re
import time
import requests

VK_API = "https://api.vk.com/method/"
VK_VERSION = "5.199"


class VKError(Exception):
    def __init__(self, code, msg):
        super().__init__(f"VK error {code}: {msg}")
        self.code = code


def html_to_plain(text: str) -> str:
    """ВК не понимает HTML: убираем теги, возвращаем сущности."""
    t = re.sub(r"<br\s*/?>", "\n", text)
    t = re.sub(r"<[^>]+>", "", t)
    return t.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")


def _call(method, token, params=None, files=None, retries=3, logger=None):
    data = dict(params or {})
    data.update({"access_token": token, "v": VK_VERSION})
    last = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.post(VK_API + method, data=data, files=files, timeout=60)
            j = r.json()
            if "error" in j:
                err = j["error"]
                code = err.get("error_code")
                if code == 6 and attempt < retries:      # слишком часто
                    time.sleep(1.5 * attempt)
                    continue
                raise VKError(code, err.get("error_msg"))
            return j["response"]
        except VKError:
            raise
        except Exception as e:                            # сеть, не-JSON
            last = e
            if logger:
                logger.warning(f"VK {method}: попытка {attempt}/{retries}: {e}")
            if attempt < retries:
                time.sleep(3 * attempt)
    raise RuntimeError(f"VK {method}: {last}")


def resolve_group_id(token, group, logger=None):
    group = str(group).strip().lstrip("-")
    if group.isdigit():
        return int(group)
    res = _call("utils.resolveScreenName", token, {"screen_name": group}, logger=logger)
    if not res or res.get("type") not in ("group", "page", "event"):
        raise RuntimeError(f"Не удалось определить id сообщества по имени «{group}»")
    return int(res["object_id"])


def _as_file(img):
    if isinstance(img, (bytes, bytearray)):          # готовые байты (JPEG из Telegram)
        return io.BytesIO(img), "photo.jpg", "image/jpeg"
    buf = io.BytesIO()                               # PIL-картинка
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf, "forecast.png", "image/png"


def _to_pil(img):
    from PIL import Image
    return img if hasattr(img, "size") and hasattr(img, "convert") else Image.open(io.BytesIO(img))


def make_collage(images, gap=12, col_width=1000):
    """Склеивает несколько картинок в одну (две колонки, как в альбоме Telegram): в пост ВК с ключом сообщества
    помещается только один документ. Порядок: каждая следующая картинка ложится в более короткую колонку."""
    from PIL import Image
    pics = [_to_pil(im).convert("RGB") for im in images]
    w = min(col_width, max(p.width for p in pics))
    pics = [p.resize((w, max(1, round(p.height * w / p.width)))) if p.width != w else p for p in pics]
    cols = [[], []]
    heights = [0, 0]
    for p in pics:
        c = 0 if heights[0] <= heights[1] else 1
        cols[c].append((p, heights[c]))
        heights[c] += p.height + gap
    ncols = 2 if cols[1] else 1
    out = Image.new("RGB", (w * ncols + gap * (ncols - 1), max(heights) - gap), "white")
    for c in range(ncols):
        for p, y in cols[c]:
            out.paste(p, (c * (w + gap), y))
    return out


def _upload_wall_doc(token, gid, img, logger=None):
    """Картинка как документ: запасной путь, если фото на стену ключом сообщества не загрузить."""
    srv = _call("docs.getWallUploadServer", token, {"group_id": gid}, logger=logger)
    buf, name, mime = _as_file(img)
    up = requests.post(srv["upload_url"], files={"file": (name, buf, mime)}, timeout=90).json()
    if not up.get("file"):
        raise RuntimeError(f"ВК не принял документ: {up}")
    saved = _call("docs.save", token, {"file": up["file"], "title": name}, logger=logger)
    d = saved["doc"] if isinstance(saved, dict) and "doc" in saved else saved[0]
    return f"doc{d['owner_id']}_{d['id']}"


def _upload_wall_photo(token, gid, img, logger=None):
    srv = _call("photos.getWallUploadServer", token, {"group_id": gid}, logger=logger)
    buf, name, mime = _as_file(img)
    up = requests.post(srv["upload_url"], files={"photo": (name, buf, mime)}, timeout=90).json()
    if not up.get("photo") or up.get("photo") == "[]":
        raise RuntimeError(f"ВК не принял картинку: {up}")
    saved = _call("photos.saveWallPhoto", token,
                  {"group_id": gid, "photo": up["photo"], "server": up["server"], "hash": up["hash"]},
                  logger=logger)
    p = saved[0]
    return f"photo{p['owner_id']}_{p['id']}"


def mirror_to_vk(images, caption_html, logger):
    """Публикует картинки и подпись на стене группы.
    Возвращает True (опубликовано), False (ошибка), None (ВК не настроен)."""
    token, group = os.getenv("VK_TOKEN"), os.getenv("VK_GROUP_ID")
    if not token or not group:
        logger.info("VK_TOKEN/VK_GROUP_ID не заданы — зеркало ВК пропущено")
        return None
    try:
        gid = resolve_group_id(token, group, logger)
        atts = [_upload_wall_photo(token, gid, im, logger) for im in images[:10]]
        res = _call("wall.post", token, {
            "owner_id": -gid, "from_group": 1,
            "message": html_to_plain(caption_html),
            "attachments": ",".join(atts),
        }, logger=logger)
        logger.info(f"✅ Пост опубликован в ВК (post_id={res.get('post_id')})")
        return True
    except Exception as e:
        logger.error(f"❌ Зеркало ВК не удалось: {e}")
        return False


_BAD_ROUTES = set()      # пути загрузки, которые в этом запуске уже отказали (код 27 и т.п.)
_PERMANENT = (3, 5, 7, 15, 27)
_DOC_LIMIT = [None]      # сколько документов ВК принимает в одном посте (определяется на первой ошибке)


def post_to_vk(images, text, logger, link=None):
    """Пост на стену группы: text — простой текст, images — PIL-картинки или bytes, link — адрес поста в Telegram.
    Картинки грузятся по цепочке: фото токеном пользователя (если задан VK_USER_TOKEN) -> фото ключом сообщества ->
    документы -> без картинок (тогда в текст дописывается ссылка на оригинал). Если ВК принимает меньше документов,
    чем картинок, лишние отбрасываются, а в текст дописывается ссылка на оригинал. Бросает исключение при ошибке."""
    token, group = os.getenv("VK_TOKEN"), os.getenv("VK_GROUP_ID")
    utoken = os.getenv("VK_USER_TOKEN")
    if not token or not group:
        raise RuntimeError("VK_TOKEN/VK_GROUP_ID не заданы")
    gid = resolve_group_id(token, group, logger)
    atts, kind = [], None
    n_images = len(images)                      # сколько картинок считаем «перенесёнными целиком»
    if images:
        routes = []
        if utoken:
            routes.append(("фото (токен пользователя)", "photo", lambda im: _upload_wall_photo(utoken, gid, im, logger)))
        routes += [("фото", "photo", lambda im: _upload_wall_photo(token, gid, im, logger)),
                   ("документы", "doc", lambda im: _upload_wall_doc(token, gid, im, logger))]
        for name, k, up in routes:
            if name in _BAD_ROUTES:
                continue
            try:
                imgs = images[:10]
                if k == "doc" and len(imgs) > 1:
                    # ключ сообщества: в посте один документ -> весь альбом одной картинкой
                    try:
                        imgs = [make_collage(imgs)]
                    except Exception as e:                       # не удалось склеить: хотя бы первая картинка
                        logger.warning(f"Склейка альбома не удалась ({e}); берём первую картинку")
                        imgs = imgs[:1]
                    n_images = 1
                atts, kind = [up(im) for im in imgs], k
                break
            except VKError as e:
                if e.code not in _PERMANENT:
                    raise
                _BAD_ROUTES.add(name)
                logger.warning(f"ВК: путь «{name}» недоступен ({e}); пробую следующий")
        if not atts:
            logger.warning("ВК: картинки перенести не удалось, в пост добавлена ссылка на оригинал")

    def publish(att_list):
        msg = text
        if images and len(att_list) < n_images and link:
            # часть картинок не перенесена (или ни одной): ссылка на оригинал в текст, вложением её нельзя
            msg = (text + "\n\n" if text else "") + f"📷 Все фото: {link}"
        params = {"owner_id": -gid, "from_group": 1, "message": msg}
        if att_list:
            params["attachments"] = ",".join(att_list)
        return _call("wall.post", token, params, logger=logger)

    k = len(atts)
    while True:
        try:
            res = publish(atts[:k])
            break
        except VKError as e:
            if kind == "doc" and e.code == 100 and "doc attachments" in str(e) and k > 0:
                k -= 1                                   # ВК принимает меньше документов в одном посте (до нуля)
                continue
            raise
    if kind == "doc" and k < len(atts):
        _DOC_LIMIT[0] = k
        logger.warning(f"ВК принимает не более {k} документов в посте: остальные картинки не вложены")
    return res.get("post_id")


def check_vk(logger):
    """Проверка токена и способов загрузки картинок без публикации.
    Возвращает False только если сообщество недоступно; отсутствие загрузки картинок — предупреждение."""
    token, group = os.getenv("VK_TOKEN"), os.getenv("VK_GROUP_ID")
    if not token or not group:
        logger.error("VK_TOKEN или VK_GROUP_ID не заданы в Secrets")
        return False
    try:
        gid = resolve_group_id(token, group, logger)
        info = _call("groups.getById", token, {"group_id": gid}, logger=logger)
        g = info["groups"][0] if isinstance(info, dict) else info[0]
        logger.info(f"Сообщество: «{g.get('name')}» (id {gid})")
    except Exception as e:
        logger.error(f"❌ Проверка ВК не пройдена: {e}")
        return False
    probes = [("фото на стену ключом сообщества", token, "photos.getWallUploadServer", {"group_id": gid}),
              ("документы ключом сообщества (картинка как документ)", token, "docs.getWallUploadServer", {"group_id": gid})]
    if os.getenv("VK_USER_TOKEN"):
        probes.insert(0, ("фото на стену токеном пользователя", os.getenv("VK_USER_TOKEN"),
                          "photos.getWallUploadServer", {"group_id": gid}))
    ok_any = False
    for label, tk, method, prm in probes:
        try:
            _call(method, tk, prm, retries=1, logger=logger)
            logger.info(f"✅ Доступно: {label}")
            ok_any = True
        except Exception as e:
            logger.warning(f"⚠ Недоступно: {label}: {e}")
    try:
        _call("wall.get", token, {"owner_id": -gid, "count": 1}, retries=1, logger=logger)
        logger.info("✅ Стена читается")
    except Exception as e:
        logger.warning(f"⚠ wall.get: {e} (публикация текста может всё равно работать)")
    if not ok_any:
        logger.warning("⚠ Картинки в ВК перенести нельзя: посты с фото уйдут текстом со ссылкой на Telegram")
    logger.info("Публикация текста проверится только реальным постом (проверка ничего не публикует)")
    return True


# ===================== MAX (platform-api2.max.ru) =====================
MAX_API = "https://platform-api2.max.ru"
MAX_TEXT_LIMIT = 4000


def _max_verify():
    """Корневой сертификат Минцифры: если TLS не проходит, положите PEM и укажите путь в MAX_CA_FILE."""
    return os.getenv("MAX_CA_FILE") or True


def split_text(text, limit=MAX_TEXT_LIMIT):
    """Режем длинный текст по абзацам/строкам, чтобы каждая часть влезла в лимит."""
    parts, cur = [], ""
    for para in text.split("\n"):
        while len(para) > limit:                      # одна строка длиннее лимита
            if cur:
                parts.append(cur); cur = ""
            parts.append(para[:limit]); para = para[limit:]
        add = para if not cur else cur + "\n" + para
        if len(add) > limit:
            parts.append(cur); cur = para
        else:
            cur = add
    if cur or not parts:
        parts.append(cur)
    return parts


def _max_req(method, path, token, logger=None, retries=3, **kw):
    last = None
    for attempt in range(1, retries + 1):
        try:
            r = requests.request(method, MAX_API + path, headers={"Authorization": token},
                                 timeout=60, verify=_max_verify(), **kw)
            if r.status_code == 429 and attempt < retries:
                time.sleep(2 * attempt); continue
            if r.status_code >= 500 and attempt < retries:
                time.sleep(3 * attempt); continue
            return r
        except requests.exceptions.SSLError:
            raise RuntimeError("TLS-ошибка при обращении к Макс: нужен корневой сертификат Минцифры (MAX_CA_FILE)")
        except Exception as e:
            last = e
            if logger:
                logger.warning(f"MAX {path}: попытка {attempt}/{retries}: {e}")
            if attempt < retries:
                time.sleep(3 * attempt)
    raise RuntimeError(f"MAX {path}: {last}")


def _max_upload_image(token, data, logger=None):
    r = _max_req("POST", "/uploads?type=image", token, logger)
    if r.status_code != 200:
        raise RuntimeError(f"MAX /uploads: {r.status_code} {r.text[:200]}")
    url = r.json()["url"]
    if isinstance(data, (bytes, bytearray)):
        blob, name, mime = bytes(data), "photo.jpg", "image/jpeg"
    else:
        b = io.BytesIO(); data.save(b, format="PNG"); blob, name, mime = b.getvalue(), "forecast.png", "image/png"
    up = requests.post(url, headers={"Authorization": token}, files={"data": (name, blob, mime)},
                       timeout=90, verify=_max_verify())
    if up.status_code != 200:
        raise RuntimeError(f"MAX upload: {up.status_code} {up.text[:200]}")
    photos = up.json().get("photos") or {}
    for v in photos.values():
        if v.get("token"):
            return v["token"]
    raise RuntimeError(f"MAX: в ответе загрузки нет token: {str(up.text)[:200]}")


def _max_send(token, chat_id, text, tokens, logger=None):
    body = {"text": text}
    if tokens:
        body["attachments"] = [{"type": "image", "payload": {"token": t}} for t in tokens]
    for attempt in range(6):                          # attachment.not.ready: ждём и повторяем
        r = _max_req("POST", f"/messages?chat_id={chat_id}", token, logger, json=body)
        if r.status_code == 200:
            return r.json()
        if "attachment.not.ready" in r.text and attempt < 5:
            time.sleep(1.5 * (2 ** attempt)); continue
        raise RuntimeError(f"MAX /messages: {r.status_code} {r.text[:300]}")


def post_to_max(images, text, logger, link=None):
    """Пост в канал Макс: картинки (до 12) + текст, длинный текст — продолжением отдельными сообщениями."""
    token, chat = os.getenv("MAX_TOKEN"), os.getenv("MAX_CHAT_ID")
    if not token or not chat:
        raise RuntimeError("MAX_TOKEN/MAX_CHAT_ID не заданы")
    tokens = [_max_upload_image(token, im, logger) for im in images[:12]]
    parts = split_text(text)
    first = _max_send(token, chat, parts[0], tokens, logger)
    for extra in parts[1:]:
        time.sleep(0.6)                               # не чаще 2 сообщений в секунду
        _max_send(token, chat, extra, [], logger)
    return (first.get("message") or first).get("body", {}).get("mid")


def check_max(logger):
    token, chat = os.getenv("MAX_TOKEN"), os.getenv("MAX_CHAT_ID")
    if not token or not chat:
        logger.info("MAX_TOKEN/MAX_CHAT_ID не заданы — Макс пропущен")
        return None
    try:
        r = _max_req("GET", "/me", token, logger)
        if r.status_code != 200:
            raise RuntimeError(f"/me: {r.status_code} {r.text[:200]}")
        logger.info(f"Макс: бот «{r.json().get('name') or r.json().get('first_name')}» доступен")
        r = _max_req("GET", f"/chats/{chat}", token, logger)
        if r.status_code != 200:
            raise RuntimeError(f"/chats/{chat}: {r.status_code} {r.text[:200]}")
        logger.info(f"✅ Канал Макс найден: «{r.json().get('title')}»")
        return True
    except Exception as e:
        logger.error(f"❌ Проверка Макс не пройдена: {e}")
        return False
