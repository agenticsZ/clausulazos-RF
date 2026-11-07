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
import math
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
APP_VERSION = "4.0.3"     # cabecera X-App-Version que envia la web
# Nombre que se muestra en los avisos para cada grupo (id -> nombre)
GROUP_NAMES = {
    "8002": "MAXX 00",
    "7362": "MAXX 01",
    "456": "TAU",
    "8183": "SHIFT",
    "6840": "NBA",
    "5436": "MAXX 02",
    "7741": "MAXX 03",
    "5448": "MAXX 04",
    "6979": "MAXX 05",
    "7162": "TXUS",
    "6289": "RIVAL",
    "7411": "EUROPA",
    # ACB
    "4053": "ACB 00",
    "2142": "ACB 01",
    "5034": "ACB 02",
    "5068": "ACB 03",
    "8798": "ACB 04",
    "9723": "ACB 05",
    "6279": "ACB 06",
    "8284": "ACB 07",
    "5783": "ACB 08",
    "5298": "ACB 09",
    "11862": "ACB BOOM",
    "11990": "ACB SHOW",
}
MAX_PAGES = 10            # paginas de 25 noticias como maximo por grupo y ejecucion
# Aviso de ofertas altas (importe en euros). Se puede cambiar con la variable RF_OFFER_MIN.
OFFER_MIN = int(os.environ.get("RF_OFFER_MIN") or 1_200_000)
# Ofertas por jugadores TUYOS: segun el % de tu clausula que representa la oferta,
# se recomienda subirla. (desde, hasta, subida): "desde" incluido y "hasta" excluido.
RAISE_BANDS = [(0.60, 0.80, 0.10), (0.80, 1.00, 0.15)]
CLAUSE_STEP = 10_000  # la clausula recomendada se redondea hacia arriba a este multiplo
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
    h = norm(haystack)
    return "clausul" in h or "clause" in h


OFFER_RE = re.compile(
    r"^(?:El\s+)?(?P<by>.+?)\s+ha hecho una oferta al\s+(?P<to>.+?)\s+por\s+"
    r"(?P<amount>\d[\d.]*)\s*€(?:\s*\+\s*\((?P<swap>[^)]*)\))?\s+por\s+(?P<player>.+)$"
)


def parse_offer(notice):
    """Datos de la oferta (cualquier importe), o None si la noticia no es una oferta."""
    if notice.get("type") != 10 and norm(notice.get("title", "")) != "oferta":
        return None
    m = OFFER_RE.match(html_to_text(notice.get("text", "")))
    if not m:
        return None
    return {"by": m.group("by"), "to": m.group("to"),
            "amount": int(m.group("amount").replace(".", "")),
            "swap": m.group("swap"), "player": m.group("player")}


def clausulazos_activos(team):
    """True / False / None (no se sabe).
    El juego lo indica en protectedAlert de cada jugador:
      'Clausulazos activados...'   -> activos
      'Clausulazos desactivados.'  -> desactivados
      'Cuidado! ... protegido ...' -> proteccion de ESE jugador (no cuenta)"""
    activos = desactivados = False
    for pl in (team or {}).get("players", []):
        alert = pl.get("protectedAlert") or {}
        text = norm(alert.get("text", ""))
        if "desactivad" in text:
            desactivados = True
        elif "clausulazos activados" in text:
            activos = True
    if activos:
        return True
    if desactivados:
        return False
    return None


def my_player(team, offer):
    """(es_a_mi_equipo, jugador). El jugador es el dict de tu plantilla o None."""
    if not team or norm(offer["to"]) != norm(team.get("name", "")):
        return False, None
    for pl in team.get("players", []):
        if norm(pl.get("fullName", "")) == norm(offer["player"]):
            return True, pl
    return True, None


def raise_advice(clause, amount):
    """(porcentaje de la clausula, consejo). Consejo vacio si la oferta aun esta lejos."""
    ratio = amount / clause
    if ratio >= 1:
        return ratio, "🚨 La oferta ya iguala o supera la cláusula"
    for low, high, up in RAISE_BANDS:
        if low <= ratio < high:
            new = math.ceil(clause * (1 + up) / CLAUSE_STEP) * CLAUSE_STEP
            return ratio, f"📈 Sube la cláusula un {up:.0%}: de {euros(clause)} a {euros(new)}"
    return ratio, ""


def euros(n):
    return f"{n:,}".replace(",", ".") + " €"


def parse_groups(raw):
    """Extrae (host, id) de cualquier URL con forma https://host/<id>/...
    sin importar separadores ni caracteres alrededor. Sin duplicados."""
    groups = []
    for m in re.finditer(r"https?://([^/\s\"'<>()\[\]`,]+)/(\d+)", raw or ""):
        g = (m.group(1), m.group(2))
        if g not in groups:
            groups.append(g)
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
    # La API exige ademas un token Bearer: se pide ya aqui para no recibir
    # un 401 en la primera peticion de cada ejecucion.
    try:
        t = s.get(f"https://{host}/api/token",
                  headers={"Accept": "application/json",
                           "Accept-Language": "es-ES,es;q=0.9",
                           "X-Requested-With": "XMLHttpRequest",
                           "X-App-Version": os.environ.get("RF_APP_VERSION") or APP_VERSION,
                           "X-Platform": "web",
                           "X-Timezone": "Europe/Madrid"},
                  timeout=30)
        token = find_token(t.json()) if t.ok else None
        if token:
            _bearer[host] = token
        else:
            print(f"[{host}] /api/token HTTP {t.status_code}: sin token")
    except Exception as e:
        print(f"[{host}] /api/token fallo: {e}")


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


def api_headers(s, host, group):
    headers = {"Accept": "application/json",
               "Accept-Language": "es-ES,es;q=0.9",
               "X-Requested-With": "XMLHttpRequest",
               "X-App-Version": os.environ.get("RF_APP_VERSION") or APP_VERSION,
               "X-Platform": "web",
               "X-Timezone": "Europe/Madrid",
               "Referer": f"https://{host}/{group}/notices"}
    x = xsrf(s, host)
    if x:
        headers["X-XSRF-TOKEN"] = x
    if host in _bearer:
        headers["Authorization"] = f"Bearer {_bearer[host]}"
    return headers


def fetch_team(s, host, group):
    """Tu equipo en ese grupo: nombre y plantilla con sus clausulas.
    El numero del grupo en la URL es el id de tu propio equipo."""
    r = s.get(f"https://{host}/api/v1/teams/{group}/{group}",
              headers=api_headers(s, host, group), timeout=30)
    if not r.ok:
        raise RuntimeError(f"HTTP {r.status_code}: {r.text[:150]!r}")
    return r.json()["data"]["team"]


def fetch_notices(s, host, group, page=1):
    url = f"https://{host}/api/v1/notices/{group}"
    params = {"search": "", "perPage": 25, "page": page}
    headers = api_headers(s, host, group)

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
    raw_groups = os.environ.get("RF_GROUPS", "")
    groups = parse_groups(raw_groups)
    if not groups:
        print(f"RF_GROUPS vacio o con formato incorrecto "
              f"(recibidos {len(raw_groups.strip())} caracteres, 0 URLs validas). "
              "Comprueba que check.yml lee RF_GROUPS de donde lo hayas guardado "
              "(secrets.RF_GROUPS o vars.RF_GROUPS) y que contiene las URLs.")
        sys.exit(1)
    print(f"Grupos configurados: {len(groups)}")

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
    teams = {}
    last_err = ""
    for host, group in groups:
        label = GROUP_NAMES.get(group, f"{host.split('.')[0]} · grupo {group}")
        key = group_key(host, group)
        first_time = key not in state
        seen = set(state.get(key, []))

        try:
            if host not in ready:
                open_host(s, host, group)
                ready.add(host)
            notices = []
            for page in range(1, MAX_PAGES + 1):
                batch = fetch_notices(s, host, group, page)
                notices.extend(batch)
                # Paramos si es la primera vez, si no hay mas noticias o si
                # ya aparece alguna conocida (las noticias vienen de nuevas a viejas)
                if first_time or not batch or any(n["id"] in seen for n in batch):
                    break
                time.sleep(0.5)
        except Exception as e:
            print(f"Error en {label}: {e}")
            errors.append(label)
            last_err = str(e)
            continue

        started += first_time
        new_count = hits = big = muted = 0

        for n in sorted(notices, key=lambda x: x["id"]):  # de antigua a reciente
            nid = n["id"]
            if nid in seen:
                continue
            seen.add(nid)
            new_count += 1
            if first_time:
                continue  # primera vez: solo se registra, sin avisar
            link = f"https://{host}/{group}/notices"
            if matches(n):
                hits += 1
                text = html_to_text(n.get("text", ""))
                msg = (f"🚨 {n.get('title', 'Noticia')} · {label}\n{text}\n"
                       f"🕒 {n.get('date', '')}\n{link}")
            else:
                offer = parse_offer(n)
                if not offer:
                    continue
                key_t = (host, group)
                if key_t not in teams:
                    try:
                        teams[key_t] = fetch_team(s, host, group)
                    except Exception as e:
                        print(f"No se pudo leer tu equipo en {label}: {e}")
                        teams[key_t] = None
                team = teams[key_t]

                es_alta = offer["amount"] >= OFFER_MIN                 # regla 1
                a_mi_equipo, jugador = my_player(team, offer)
                clausula = (jugador or {}).get("clause")
                ratio, consejo = (raise_advice(clausula, offer["amount"])
                                  if clausula else (None, ""))
                cerca = ratio is not None and ratio >= RAISE_BANDS[0][0]  # regla 2
                if not (es_alta or cerca):
                    continue

                estado = clausulazos_activos(team)
                if estado is False:
                    muted += 1
                    continue  # clausulazos desactivados en este grupo: no se avisa
                big += 1

                extra = f" (+ {offer['swap']})" if offer["swap"] else ""
                lines = [f"{'💰 Oferta alta' if es_alta else '🎯 Oferta por un jugador tuyo'} · {label}",
                         f"{offer['by']} → {offer['to']}: {euros(offer['amount'])}{extra} "
                         f"por {offer['player']}",
                         "✅ Clausulazos activados" if estado
                         else "❔ No pude comprobar si los clausulazos están activos"]
                if a_mi_equipo and es_alta:
                    lines.append("🎯 Es un jugador de tu equipo")
                if clausula:
                    lines.append(f"Cláusula: {euros(clausula)} · la oferta es el {ratio:.0%}")
                if consejo:
                    lines.append(consejo)
                lines += [f"🕒 {n.get('date', '')}", link]
                msg = "\n".join(lines)
            ok = tg(msg)
            if ok:
                sent += 1
            else:
                seen.discard(nid)  # reintentar en la proxima ejecucion

        print(f"{label}: {len(notices)} leidas, {new_count} nuevas, {hits} con clausula, {big} avisos de oferta"
              f"{f' (+{muted} sin avisar: clausulazos desactivados)' if muted else ''}"
              f"{' (primera vez)' if first_time else ''}")

        state[key] = sorted(seen, reverse=True)[:KEEP_IDS]
        time.sleep(1)

    if started:
        tg(f"✅ Monitor iniciado: {started} grupo(s) registrados. "
           "Desde ahora recibirás avisos de las noticias nuevas.")
    if errors:
        alert(f"fallo la lectura de {len(errors)} grupo(s). Ultimo error: {last_err[:200]}")

    save_state(state)
    print(f"Grupos: {len(groups)} · avisos enviados: {sent} · errores: {len(errors)}")


if __name__ == "__main__":
    main()
