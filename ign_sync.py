"""
Niven Sismo · Sincronización automática con el catálogo del IGN.

Pide al buscador del catálogo del IGN los sismos de los últimos días dentro
del recuadro de la Vega de Granada (incluidos los de magnitud < 1,5, que EMSC
no publica) y los guarda en Firebase Realtime Database en /ign/events.

Variables de entorno (secretos de GitHub):
  FIREBASE_DB_URL       p. ej. https://nivensismo-default-rtdb.europe-west1.firebasedatabase.app
  FIREBASE_SA_JSON      contenido completo del JSON de la cuenta de servicio
Opcionales:
  IGN_DAYS              días hacia atrás que se piden (por defecto 3)
  DRY_RUN=1             no escribe en Firebase, solo muestra lo encontrado
"""

import io
import json
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pandas as pd
import requests

# Recuadro amplio de la Vega de Granada
LAT_MIN, LAT_MAX = 37.05, 37.30
LON_MIN, LON_MAX = -3.85, -3.50

IGN_URL = (
    "https://www.ign.es/web/ign/portal/sis-catalogo-terremotos/-/"
    "catalogo-terremotos/searchTerremoto"
)
MADRID = ZoneInfo("Europe/Madrid")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (NivenSismo; sincronizacion catalogo IGN)",
    "Accept-Language": "es-ES,es;q=0.9",
}


def fetch_html(day, box) -> str:
    """Pide un día y un recuadro (el IGN pagina los resultados de 50 en 50)."""
    la0, la1, lo0, lo1 = box
    params = {
        "latMin": round(la0, 5), "latMax": round(la1, 5),
        "longMin": round(lo0, 5), "longMax": round(lo1, 5),
        "startDate": day.strftime("%d/%m/%Y"),
        "endDate": day.strftime("%d/%m/%Y"),
        "selIntensidad": "N", "selMagnitud": "N",
        "intMin": "", "intMax": "", "magMin": "", "magMax": "",
        "selProf": "N", "profMin": "", "profMax": "",
        "fases": "no", "cond": "",
    }
    last_err = None
    for espera in (0, 15, 30, 60, 90):  # el IGN a veces da 502 si se le pide mucho
        time.sleep(espera)
        try:
            r = requests.get(IGN_URL, params=params, headers=HEADERS, timeout=60)
            r.raise_for_status()
            return r.text
        except requests.RequestException as e:
            last_err = e
    raise RuntimeError(f"No se pudo descargar el catálogo del IGN: {last_err}")


def _num(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return None
    s = str(v).strip().replace(",", ".")
    if s in ("", "-", "nan"):
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _txt(v):
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return ""
    return str(v).strip()


def parse(html: str) -> dict:
    """Devuelve {evid: {...}} a partir de la tabla de resultados."""
    try:
        tablas = pd.read_html(io.StringIO(html), match="Evento", flavor="lxml")
    except ValueError:
        return {}  # sin tabla: ningún sismo en el periodo, o página cambiada

    eventos = {}
    for df in tablas:
        cols = {c: str(c).strip().lower() for c in df.columns}
        df = df.rename(columns=cols)

        def col(*claves):
            for c in df.columns:
                if all(k in c for k in claves):
                    return c
            return None

        c_ev, c_fecha = col("evento"), col("fecha")
        c_hora = col("hora", "utc")
        c_lat, c_lon = col("latitud"), col("longitud")
        c_prof, c_mag = col("prof"), col("magnitud")
        c_tipo, c_int = col("tipo"), col("int")
        c_loc = col("locali")
        if not (c_ev and c_fecha and c_hora and c_lat and c_lon):
            continue

        for _, f in df.iterrows():
            evid = _txt(f[c_ev])
            if not evid.startswith("es"):
                continue
            try:
                dt = datetime.strptime(
                    f"{_txt(f[c_fecha])} {_txt(f[c_hora])}", "%d/%m/%Y %H:%M:%S"
                ).replace(tzinfo=timezone.utc)
            except ValueError:
                continue
            lat, lon = _num(f[c_lat]), _num(f[c_lon])
            if lat is None or lon is None:
                continue
            if not (LAT_MIN <= lat <= LAT_MAX and LON_MIN <= lon <= LON_MAX):
                continue
            mag = _num(f[c_mag]) if c_mag else None
            # Se guardan todas las magnitudes: los <1,5 son los microsismos que
            # EMSC no publica; los ≥1,5 sirven de respaldo si EMSC aún no los tiene
            if mag is None:
                continue
            eventos[evid] = {
                "t": int(dt.timestamp() * 1000),  # epoch ms UTC
                "lat": lat,
                "lon": lon,
                "depth": _num(f[c_prof]) if c_prof else None,
                "mag": mag,
                "magType": _txt(f[c_tipo]) if c_tipo else "",
                "int": _txt(f[c_int]) if c_int and c_int != c_tipo else "",
                "loc": _txt(f[c_loc]) if c_loc else "",
                "src": "IGN",
            }
    return eventos


PAGINA = 50  # filas por página del buscador del IGN


def filas(html: str) -> int:
    """Número de sismos que muestra la tabla de resultados."""
    try:
        tablas = pd.read_html(io.StringIO(html), match="Evento", flavor="lxml")
    except ValueError:
        return 0
    return max(len(t) for t in tablas)


def fetch_dia(day, box=(LAT_MIN, LAT_MAX, LON_MIN, LON_MAX), nivel=0) -> dict:
    """Si un recuadro llena la página (50 filas), lo parte en 4 y repite.
    Así se recogen todos los sismos aunque un día haya cientos."""
    html = fetch_html(day, box)
    time.sleep(3)  # sin prisas con el servidor del IGN
    if filas(html) >= PAGINA and nivel < 6:
        la0, la1, lo0, lo1 = box
        lam, lom = (la0 + la1) / 2, (lo0 + lo1) / 2
        eventos = {}
        for sub in ((la0, lam, lo0, lom), (la0, lam, lom, lo1),
                    (lam, la1, lo0, lom), (lam, la1, lom, lo1)):
            eventos.update(fetch_dia(day, sub, nivel + 1))
        return eventos
    if filas(html) >= PAGINA:
        print(f"AVISO: {day:%d/%m} sigue con 50 filas en un recuadro mínimo.")
    return parse(html)


def firebase_token(sa_json: str) -> str:
    from google.oauth2 import service_account
    from google.auth.transport.requests import Request

    creds = service_account.Credentials.from_service_account_info(
        json.loads(sa_json),
        scopes=[
            "https://www.googleapis.com/auth/firebase.database",
            "https://www.googleapis.com/auth/userinfo.email",
        ],
    )
    creds.refresh(Request())
    return creds.token


def main() -> int:
    days = int(os.environ.get("IGN_DAYS", "3"))
    dry = os.environ.get("DRY_RUN") == "1"

    today = datetime.now(MADRID).date()
    auth = db = None
    if not dry:
        db = os.environ["FIREBASE_DB_URL"].rstrip("/")
        auth = {"access_token": firebase_token(os.environ["FIREBASE_SA_JSON"])}

    def guardar(evs: dict):
        """Guarda en Firebase según avanza, para no perder lo ya descargado."""
        if dry or not evs:
            return
        lote = list(evs.items())
        for k in range(0, len(lote), 500):
            requests.patch(f"{db}/ign/events.json", params=auth,
                           json=dict(lote[k:k + 500]), timeout=60).raise_for_status()

    eventos, fallidos = {}, []
    dias = [today - timedelta(days=i) for i in range(days, -1, -1)]
    for ronda in (1, 2):
        pendientes = dias if ronda == 1 else fallidos
        if ronda == 2:
            if not fallidos:
                break
            print(f"Reintentando {len(fallidos)} día(s) tras una pausa…")
            time.sleep(120)
            fallidos = []
        for day in pendientes:
            try:
                del_dia = fetch_dia(day)
            except RuntimeError as e:
                print(f"  {day:%d/%m}: FALLO ({e.__class__.__name__}: IGN no responde)")
                fallidos.append(day)
                continue
            print(f"  {day:%d/%m}: {len(del_dia)} sismos")
            eventos.update(del_dia)
            guardar(del_dia)
    print(f"IGN: {len(eventos)} sismos en el recuadro (hoy y {days} días atrás), "
          f"{sum(1 for e in eventos.values() if e['mag'] < 1.5)} microsismos")
    if fallidos:
        print("Días sin descargar (vuelve a lanzarlo más tarde): "
              + ", ".join(f"{d:%d/%m}" for d in fallidos))
    if eventos:
        mags = [e["mag"] for e in eventos.values() if e["mag"] is not None]
        ult = max(e["t"] for e in eventos.values())
        print(f"  magnitud mín/máx: {min(mags)} / {max(mags)}")
        print(f"  más reciente: {datetime.fromtimestamp(ult/1000, MADRID):%d/%m %H:%M} (hora local)")
    else:
        print("AVISO: 0 microsismos; si se repite, quizá el IGN ha cambiado su página.")

    if dry:
        print(json.dumps(list(eventos.items())[:3], ensure_ascii=False, indent=1))
        return 0

    meta = {
        "lastRun": int(time.time() * 1000),
        "count": len(eventos),
        "days": days,
        "ok": not fallidos,
    }
    requests.put(f"{db}/ign/meta.json", params=auth, json=meta, timeout=30).raise_for_status()
    print("Firebase actualizado.")
    return 1 if fallidos else 0


if __name__ == "__main__":
    sys.exit(main())
