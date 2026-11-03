"""
Vigila las noticias de grupos de Ring Fantasy y avisa por Telegram
cuando una noticia (titulo o texto) menciona "clausula" / "clausulazo".

Variables de entorno (Secrets de GitHub):
  TG_TOKEN, TG_CHAT_ID, RF_USER, RF_PASS, RF_GROUPS
RF_GROUPS: URLs de los grupos, una por linea o separadas por comas, p. ej.
  https://euroleague.ringfantasybasketball.com/8002/notices
"""
import hashlib
import json
import os
import re
import sys
import time
import unicodedata
from urllib.parse import unquote, urlparse

import requests
from bs4 import BeautifulSoup

MAIN = "https://ringfantasybasketball.com"
STATE_FILE = "seen.json"
KEEP_IDS = 300            # ids recordados por grupo
ALERT_EVERY = 6 * 3600    # segundos minimos entre avisos de error
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36")

_bearer = {}  # host -> token (solo si la API lo exige)
_diag = set()  # hosts ya diagnosticados


# ---------- utilidades ----------
def norm(text):
    text = unicodedata.normalize("NFD", text.lower())
    return "".join(c for c in text if unicodedata.category(c) != "Mn")


def html_to_text(html):
    return BeautifulSoup(html or "", "html.parser").get_text(" ", strip=True)


def matches(notice):
    """Busca solo en titulo y texto. NO en el resto del JSON, porque
    player.protectedAlert menciona 'clausulazos' en casi todas las noticias."""
    haystack = f"{notice.get('title', '')} {html_to_text(notice.get('text', ''))}"
    return "clausul" in norm(haystack)


def parse_groups(raw):
    groups = []
    for item in re.split(r"[\s,]+", raw.strip()):
        m = re.match(r"https?://([^/]+)/(\d+)", item)
        if m:
            groups.append((m.group(1), m.group(2)))
    return groups


def group_key(host, group):
    # Hash para no dejar ids de grupos legibles en un repo publico
    return hashlib.sha256(f"{host}/{group}".encode()).hexdigest()[:12]


def tg(msg):
    r = requests.post(
        f"https://api.telegram.org/bot{os.environ['TG_TOKEN']}/sendMessage",
        data={"chat_id": os.environ["TG_CHAT_ID"], "text": msg[:4000],
              "disable_web_page_preview": True},
        timeout=20,
    )
    if not r.ok:
        print(f"Telegram devolvio HTTP {r.status_code}")
    return r.ok


def load_state():
    try:
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f)


# ---------- sesion ----------
def login(s):
    r = s.get(f"{MAIN}/login", timeout=30)
    r.raise_for_status()
    token = BeautifulSoup(r.text, "html.parser").find("input", {"name": "_token"})
    if not token:
        raise RuntimeError("No se encontro el _token en /login")
    r = s.post(
        f"{MAIN}/login",
        data={"_token": token["value"], "timezone": "Europe/Madrid",
              "email": os.environ["RF_USER"], "password": os.environ["RF_PASS"]},
        headers={"Referer": f"{MAIN}/login", "Origin": MAIN},
        timeout=30,
    )
    if urlparse(r.url).path.rstrip("/") == "/login":
        raise RuntimeError("Login fallido (credenciales incorrectas o captcha)")


def open_host(s, host, group):
    """Visitar la pagina del grupo completa el flujo OAuth y crea la
    cookie de sesion del subdominio."""
    r = s.get(f"https://{host}/{group}/notices", timeout=30)
    if urlparse(r.url).netloc != host:
        raise RuntimeError(f"No se pudo abrir sesion en {host}")


def xsrf(s, host):
    value = None
    for c in s.cookies:
        if c.name == "XSRF-TOKEN" and c.domain.lstrip(".") in host:
            value = c.value
    return unquote(value) if value else None


def find_token(obj):
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str) and "token" in k.lower() and len(v) > 20:
                return v
            found = find_token(v)
            if found:
                return found
    elif isinstance(obj, list):
        for v in obj:
            found = find_token(v)
            if found:
                return found
    return None


def fetch_notices(s, host, group):
    url = f"https://{host}/api/v1/notices/{group}"
    params = {"search": "", "perPage": 25, "page": 1}
    headers = {"Accept": "application/json",
               "X-Requested-With": "XMLHttpRequest",
               "Referer": f"https://{host}/{group}/notices"}
    x = xsrf(s, host)
    if x:
        headers["X-XSRF-TOKEN"] = x
    if host in _bearer:
        headers["Authorization"] = f"Bearer {_bearer[host]}"

    r = s.get(url, params=params, headers=headers, timeout=30)

    if not r.ok and host not in _diag:
        _diag.add(host)
        print(f"[{host}] API HTTP {r.status_code}: {r.text[:300]!r}")
        print(f"[{host}] cabeceras enviadas: {sorted(headers)}")
        token, keys = None, None
        try:
            t = s.get(f"https://{host}/api/token", headers=headers, timeout=30)
            body = t.json() if t.ok else None
            keys = sorted(body) if isinstance(body, dict) else type(body).__name__
            token = find_token(body) if body is not None else None
            print(f"[{host}] /api/token HTTP {t.status_code}, claves: {keys}, "
                  f"token: {'si' if token else 'no'}")
        except Exception as e:
            print(f"[{host}] /api/token fallo: {e}")
        if token:
            _bearer[host] = token
            headers["Authorization"] = f"Bearer {token}"
            r = s.get(url, params=params, headers=headers, timeout=30)
            print(f"[{host}] reintento con Bearer: HTTP {r.status_code}: {r.text[:200]!r}")

    if not r.ok:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:150]!r}")
    return r.json()["data"]["notices"]


# ---------- principal ----------
def main():
    groups = parse_groups(os.environ["RF_GROUPS"])
    if not groups:
        print("RF_GROUPS vacio o con formato incorrecto")
        sys.exit(1)

    state = load_state()
    s = requests.Session()
    s.headers["User-Agent"] = UA

    def alert(msg):
        now = time.time()
        if now - state.get("_alert", 0) > ALERT_EVERY:
            tg(f"⚠️ Monitor Ring Fantasy: {msg}")
            state["_alert"] = now

    try:
        login(s)
    except Exception as e:
        print(f"Error de login: {e}")
        alert(f"fallo el login ({e})")
        save_state(state)
        return

    ready, started, errors, sent = set(), 0, [], 0
    for host, group in groups:
        label = f"{host.split('.')[0]} · grupo {group}"
        try:
            if host not in ready:
                open_host(s, host, group)
                ready.add(host)
            notices = fetch_notices(s, host, group)
        except Exception as e:
            print(f"Error en {label}: {e}")
            errors.append(label)
            continue

        key = group_key(host, group)
        first_time = key not in state
        seen = set(state.get(key, []))
        started += first_time

        for n in notices:
            nid = n["id"]
            if nid in seen:
                continue
            seen.add(nid)
            if first_time or not matches(n):
                continue  # primera vez: solo se registra, sin avisar
            text = html_to_text(n.get("text", ""))
            ok = tg(f"🚨 {n.get('title', 'Noticia')} · {label}\n{text}\n"
                    f"🕒 {n.get('date', '')}\nhttps://{host}/{group}/notices")
            if ok:
                sent += 1
            else:
                seen.discard(nid)  # reintentar en la proxima ejecucion

        state[key] = sorted(seen, reverse=True)[:KEEP_IDS]
        time.sleep(1)

    if started:
        tg(f"✅ Monitor iniciado: {started} grupo(s) registrados. "
           "Desde ahora recibirás avisos de las noticias nuevas.")
    if errors:
        alert(f"fallo la lectura de {len(errors)} grupo(s): " + ", ".join(errors[:5]))

    save_state(state)
    print(f"Grupos: {len(groups)} · avisos enviados: {sent} · errores: {len(errors)}")


if __name__ == "__main__":
    main()
