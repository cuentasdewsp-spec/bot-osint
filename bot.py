# ----------------------------------------------------------------------------
# BOT DE TELEGRAM - CONSULTAS OSINT
# RUNT + SIMIT + CEDULA + MATRICULA + NEQUI + FOTO DNI + DNRPA + RTO + TEL (MOVISTAR)
# ----------------------------------------------------------------------------

import html
import json
import hashlib
import math
import time
import base64
import re
import logging
import urllib3
from typing import Optional, Dict, Any, List
from io import BytesIO
import asyncio

import requests
import telebot

try:
    from pysqlite3 import dbapi2 as sqlite3
except ImportError:
    import sqlite3

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# Selenium (para RTO)
from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import TimeoutException, NoSuchElementException

# Playwright (para Movistar / Pago Fácil)
from playwright.async_api import async_playwright, TimeoutError as PlaywrightTimeoutError

# ============================================================================
# CONFIGURACION Y BASE DE DATOS
# ============================================================================
TOKEN = "8971367232:AAFR3a3Ude-X5jjWxlSnMHujyztSpERJ7gI"
MI_TELEGRAM_ID = 8639936549

DB_NAME = "bot_database.db"

# Credenciales Pago Fácil / Movistar
PAGO_FACIL_EMAIL = "cuentasdewsp@gmail.com"
PAGO_FACIL_PASS = "hola3279."
PAGO_FACIL_URL = "https://pagosenlinea.pagofacil.com.ar/"


def init_db():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("CREATE TABLE IF NOT EXISTS admins (user_id INTEGER PRIMARY KEY)")
    cursor.execute("CREATE TABLE IF NOT EXISTS tokens (user_id TEXT PRIMARY KEY, tokens INTEGER, username TEXT)")
    conn.commit()
    cursor.execute("SELECT user_id FROM admins WHERE user_id = ?", (MI_TELEGRAM_ID,))
    if not cursor.fetchone():
        cursor.execute("INSERT INTO admins (user_id) VALUES (?)", (MI_TELEGRAM_ID,))
        conn.commit()
    conn.close()


init_db()


def obtener_admins():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id FROM admins")
    filas = cursor.fetchall()
    conn.close()
    return [fila[0] for fila in filas]


def agregar_admin_db(user_id):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("INSERT OR IGNORE INTO admins (user_id) VALUES (?)", (user_id,))
    conn.commit()
    conn.close()


def obtener_tokens_usuarios():
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("SELECT user_id, tokens, username FROM tokens")
    filas = cursor.fetchall()
    conn.close()
    return {str(u): {"tokens": t, "username": un} for u, t, un in filas}


def guardar_token_db(user_id, tokens, username):
    conn = sqlite3.connect(DB_NAME)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO tokens (user_id, tokens, username) VALUES (?, ?, ?)
        ON CONFLICT(user_id) DO UPDATE SET tokens = ?, username = ?
    """, (str(user_id), tokens, username, tokens, username))
    conn.commit()
    conn.close()


def verificar_y_descontar_tokens(message, costo):
    user_id = message.from_user.id
    if user_id in obtener_admins():
        return True

    user_id_str = str(user_id)
    username = f"@{message.from_user.username}" if message.from_user.username else "Sin username"
    tokens_actuales = obtener_tokens_usuarios().get(user_id_str, {}).get("tokens", 0)

    if tokens_actuales < costo:
        bot.reply_to(
            message,
            f"No suficientes tokens.\nNecesitas: {costo} | Tienes: {tokens_actuales}.",
            parse_mode="HTML"
        )
        return False

    nuevos_tokens = tokens_actuales - costo
    guardar_token_db(user_id_str, nuevos_tokens, username)
    bot.reply_to(
        message,
        f"Descontados {costo} token(s). Quedan <b>{nuevos_tokens}</b> tokens.",
        parse_mode="HTML"
    )
    return True


FOOTER_HIDDEN = "\n\n<i><tg-spoiler>Consulta procesada por OSINT Bot — Fuente autorizada</tg-spoiler></i>"


def wrap_footer(text: str) -> str:
    return text + FOOTER_HIDDEN


# ============================================================================
# URLS DE APIS
# ============================================================================
RUNT_API = "https://runtproapi.runt.gov.co/hi-solicitud-ms/historicos-vehiculares/consulta-principal-historico"
SIMIT_API = "https://consultasimit.fcm.org.co/simit/microservices/estado-cuenta-simit/estadocuenta/consulta"
CAPTCHA_API = "https://qxcaptcha.fcm.org.co/api.php"
CEDULA_API = "https://ventanillasocial.dnp.gov.co/Home/ObtenerDatosRUI"
NEQUI_API = "https://nequi-consulta-api.ohhyejin.workers.dev/nequi/"
MATRICULA_API = "https://colombia.hackpurgatory.org/colombia"
CREDICUOTAS_API = "https://clientes.credicuotas.com.ar/v1/onboarding/resolvecustomers/"
FOTODNI_API = "https://cybertg.lat/api/fotosarg/consultar"
INFORME_BA_API = "https://arxgx.sxzzz.site/buscar"
MITZUKI_NEQUI_API = "https://api.mitzuki.xyz/dox/nequi"
RTO_API = "https://www.cent.utn.edu.ar/rto/"

BASE_URL_DNRPA = 'https://www.dnrpa.gov.ar/portal_dnrpa'

UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'

HEADERS = {
    "Accept": "application/json",
    "User-Agent": UA
}

HEADERS_CEDULA = {
    "authority": "ventanillasocial.dnp.gov.co",
    "accept": "*/*",
    "content-type": "application/x-www-form-urlencoded",
    "origin": "https://ventanillasocial.dnp.gov.co",
    "referer": "https://ventanillasocial.dnp.gov.co/",
    "user-agent": UA
}

REGEX_DOMINIO_AR = re.compile(r"^[A-Z]{2,3}\d{3}[A-Z]{0,2}$")

bot = telebot.TeleBot(TOKEN)

# ============================================================================
# LOGICA MOVISTAR (PLAYWRIGHT)
# ============================================================================
async def obtener_token_movistar():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context()
        page = await context.new_page()

        token_container = {"token": None}

        async def handle_request(request):
            if "/api/billers" in request.url:
                auth_header = request.headers.get("authorization")
                if auth_header and auth_header.startswith("Bearer "):
                    token_container["token"] = auth_header.split(" ")[1]

        context.on("request", handle_request)

        try:
            await page.goto(PAGO_FACIL_URL, timeout=60000)
            await page.wait_for_selector('input[type="email"]', timeout=15000)
            await page.fill('input[type="email"]', PAGO_FACIL_EMAIL)
            await page.fill('input[type="password"]', PAGO_FACIL_PASS)
            await page.click('button:has-text("Ingresar")')

            for _ in range(30):
                await asyncio.sleep(1)
                if token_container["token"]:
                    break

            return token_container["token"]

        except PlaywrightTimeoutError:
            return None
        finally:
            await browser.close()


def consultar_movistar_api(token: str, numero: str):
    payload = {
        "recaptchaToken": "1234",
        "idBiller": 2168,
        "descripcionProducto": "Movistar",
        "origen": "PORTAL_WEB",
        "datosAdicionales": [
            {
                "ordinal": 1,
                "valor": numero,
                "descripcion": "Nro. de Celular"
            }
        ]
    }

    headers = {
        "authorization": f"Bearer {token}",
        "content-type": "application/json",
        "origin": "https://pagosenlinea.pagofacil.com.ar",
        "user-agent": "Mozilla/5.0"
    }

    response = requests.post("https://pagosenlinea.pagofacil.com.ar/api/carrito/productos", headers=headers, json=payload)

    if response.status_code == 200:
        return response.json()
    else:
        return {"error": f"Error {response.status_code}: No se pudo obtener la información."}


def ejecutar_consulta_movistar(numero: str):
    try:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        token = loop.run_until_complete(obtener_token_movistar())
        loop.close()

        if not token:
            return {"error": "No se pudo obtener el token de acceso."}

        resultado = consultar_movistar_api(token, numero)
        
        if isinstance(resultado, dict) and "error" in resultado:
            return resultado

        transacciones = resultado.get("transacciones") if isinstance(resultado, dict) else (resultado[0].get("transacciones", []) if isinstance(resultado, list) and len(resultado) > 0 else [])
        if not transacciones:
            return {"error": "No se encontraron resultados para este número."}

        opciones = transacciones[0].get("opciones", [])
        detalle = opciones[0].split(";")[-1] if opciones else "No disponible"

        return {
            "numero": numero,
            "producto": "Movistar",
            "detalle": detalle
        }
    except Exception as e:
        return {"error": str(e)}


# ============================================================================
# LOGICA DNRPA
# ============================================================================
def search_patente_dnrpa(dominio: str) -> dict:
    dominio = dominio.upper().strip()
    try:
        s = requests.Session()
        s.headers.update({
            'User-Agent': UA,
            'Referer': f'{BASE_URL_DNRPA}/radicacion2.php',
        })

        s.get(f'{BASE_URL_DNRPA}/radicacion2.php', verify=False, timeout=15)

        resp = s.post(
            f'{BASE_URL_DNRPA}/radicacion/consinve_amq.php',
            data={'dominio': dominio},
            verify=False,
            timeout=15,
        )

        if resp.status_code != 200:
            return {'error': f'Error del servidor DNRPA (status {resp.status_code})'}

        text = resp.text

        if 'no se encuentra' in text.lower() or ('error' in text.lower() and len(text) < 500):
            return {'error': f'No se encontro informacion para el dominio {dominio}.'}

        if 'Registro Seccional' not in text and 'RADICACION' not in text.upper():
            return {'error': f'No se encontro informacion para el dominio {dominio}.'}

        result = {'dominio': dominio}

        tipo_match = re.search(r'Tipo de Veh[^<]*</b>\.\s*<font[^>]*>([^<]+)', text)
        if tipo_match:
            result['tipo_vehiculo'] = html.unescape(tipo_match.group(1).strip())

        reg_match = re.search(r'Registro Seccional\.\s*</font>\s*<font[^>]*>\s*([^<]+)', text)
        if reg_match:
            result['registro_seccional'] = html.unescape(reg_match.group(1).strip())

        dir_match = re.search(r'Direcci[^.]*\.\s*</font>\s*<font[^>]*>\s*([^<]+)', text)
        if dir_match:
            result['direccion'] = html.unescape(dir_match.group(1).strip())

        loc_match = re.search(r'Localidad\.\s*</font>\s*<font[^>]*>\s*([^<]+)', text)
        if loc_match:
            result['localidad'] = html.unescape(loc_match.group(1).strip())

        prov_match = re.search(r'Provincia\.\s*</font>\s*<font[^>]*>\s*([^<]+)', text)
        if prov_match:
            result['provincia'] = html.unescape(prov_match.group(1).strip())

        cp_match = re.search(r'Postal\.\s*</font>\s*<font[^>]*>\s*([^<]+)', text)
        if cp_match:
            result['codigo_postal'] = html.unescape(cp_match.group(1).strip())

        tel_match = re.search(r'fono\.\s*</font>\s*<font[^>]*>\s*([^<]+)', text)
        if tel_match:
            result['telefono'] = html.unescape(tel_match.group(1).strip())

        return result

    except requests.exceptions.Timeout:
        return {'error': 'Timeout al conectar con DNRPA.'}
    except Exception as e:
        logging.error(f'[DNRPA] Excepcion: {e}')
        return {'error': f'Error al consultar DNRPA: {str(e)}'}


def format_dnrpa_secciones(data: dict, dominio: str) -> str:
    if "error" in data:
        return wrap_footer(f"<i>Error: {html.escape(str(data['error']))}</i>")

    lines = [
        f"<i>INFORME DNRPA - {dominio}</i>\n",
        "<i>INFORMACION DEL VEHICULO</i>"
    ]

    for clave, valor in data.items():
        if clave.lower() == "dominio":
            continue
        if valor:
            campo_fmt = str(clave).replace("_", " ").title()
            val_limpio = html.escape(str(valor))
            lines.append(f"<i>• <b>{campo_fmt}:</b> {val_limpio}</i>")

    return wrap_footer("\n".join(lines))


# ============================================================================
# LOGICA RTO (Selenium)
# ============================================================================
def consultar_rto(dominio: str) -> dict:
    options = webdriver.ChromeOptions()
    options.add_argument("--headless=new")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--disable-gpu")
    options.add_argument("--window-size=1280,900")

    driver = webdriver.Chrome(options=options)
    wait = WebDriverWait(driver, 15)
    datos = {"_estado": "ERROR", "mensaje": "Error desconocido"}

    try:
        driver.get(RTO_API)

        campo = wait.until(EC.presence_of_element_located((By.ID, "dominio")))
        campo.clear()
        campo.send_keys(dominio)

        driver.find_element(By.ID, "enviar").click()

        wait.until(EC.presence_of_element_located(
            (By.XPATH, "//h2[contains(text(), 'Resultado de la consulta')]")
        ))
        time.sleep(1)

        try:
            alert = driver.find_element(
                By.CSS_SELECTOR, "div.alert.alert-success, div.alert.alert-danger"
            )
            texto = alert.text
            clase = alert.get_attribute("class")

            if "alert-success" in clase:
                for linea in texto.split("\n"):
                    if ":" in linea:
                        k, v = linea.split(":", 1)
                        datos[k.strip()] = v.strip()
                datos["_estado"] = "OK"
            else:
                datos["_estado"] = "ERROR"
                datos["mensaje"] = texto

        except NoSuchElementException:
            datos["_estado"] = "SIN_RESULTADO"
            datos["mensaje"] = "No se encontro el bloque de resultado"

    except TimeoutException:
        datos["_estado"] = "TIMEOUT"
        datos["mensaje"] = "La pagina no cargo a tiempo"
    except Exception as e:
        datos["_estado"] = "ERROR"
        datos["mensaje"] = str(e)
    finally:
        driver.quit()

    return datos


def format_rto(data: dict, dominio: str) -> str:
    if data.get("_estado") != "OK":
        return wrap_footer(
            f"<i>RTO - {dominio}</i>\n\n"
            f"<i>{html.escape(data.get('mensaje', 'Sin datos'))}</i>"
        )

    lines = [
        f"<i>RTO - {dominio}</i>\n",
        f"<i><b>Resultado:</b> {html.escape(data.get('Resultado', '-'))}</i>",
        f"<i><b>Tipo:</b> {html.escape(data.get('Tipo de revision', '-'))}</i>",
        f"<i><b>Revision:</b> {html.escape(data.get('Fecha Revision', '-'))}</i>",
        f"<i><b>Vencimiento:</b> {html.escape(data.get('Fecha Vencimiento', '-'))}</i>",
        f"<i><b>Certificado:</b> {html.escape(data.get('Certificado', '-'))}</i>",
        f"<i><b>Centro:</b> {html.escape(data.get('Centro de Revision Tecnica', '-'))}</i>",
    ]
    return wrap_footer("\n".join(lines))


# ============================================================================
# UTILIDADES DE FORMATEO GENERAL
# ============================================================================
def format_response(data: Any) -> str:
    if isinstance(data, list):
        if not data:
            return wrap_footer("<i>Sin registros en base de datos.</i>")
        data = data[0]

    if isinstance(data, dict):
        if "error" in data:
            return wrap_footer(f"<i>Error: {html.escape(str(data['error']))}</i>")
        if not data or all(not v for v in data.values()):
            return wrap_footer("<i>Sin registros encontrados.</i>")
        
        lineas = ["<i>RESULTADO DE LA CONSULTA</i>\n"]
        for clave, valor in data.items():
            if str(clave).lower() == "creator":
                continue
            campo = str(clave).replace("_", " ").title()
            val_limpio = "Sin datos" if valor is None or valor == "" else html.escape(str(valor))
            lineas.append(f"<i>• {campo}: {val_limpio}</i>")
            
        texto_final = "\n".join(lineas)
        if len(texto_final) > 3500:
            texto_final = texto_final[:3500] + "\n<i>... (resultado recortado por extensión)</i>"
            
        return wrap_footer(texto_final)

    texto_str = str(data)
    if len(texto_str) > 3500:
        texto_str = texto_str[:3500] + "\n... (recortado)"
    return wrap_footer(f"<i>Informacion:\n{html.escape(texto_str)}</i>")


def format_telco(data: Any) -> str:
    return format_response(data)


def format_runt(data: Dict) -> str:
    veh = data.get("caracteristicasVehiculo", {})
    prop = data.get("propietarios", [{}])[0]
    lic = data.get("licenciaTransito", {})
    soat_list = data.get("soat", [])
    rtm_list = data.get("revisionTecnicoMecanica", [])
    soat_vig = next((s for s in soat_list if s.get("vigente") == "SI"), soat_list[0] if soat_list else {})
    rtm_vig = next((r for r in rtm_list if r.get("vigente") == "SI"), rtm_list[0] if rtm_list else {})

    lines = [
        "<i>DATOS DEL VEHICULO (RUNT)</i>\n",
        f"<i>Placa: {veh.get('numeroPlaca', 'N/A')}</i>",
        f"<i>Propietario: {prop.get('nombre', 'N/A')}</i>",
        f"<i>Documento: {prop.get('numDoc', 'N/A')}</i>",
        f"<i>Marca: {veh.get('marca', 'N/A')} | Linea: {veh.get('linea', 'N/A')}</i>",
        f"<i>Modelo: {veh.get('modelo', 'N/A')} | Color: {veh.get('color', 'N/A')}</i>",
        f"<i>Clase: {veh.get('clase', 'N/A')} | Servicio: {veh.get('servicio', 'N/A')}</i>",
        f"<i>Combustible: {veh.get('tipoCombustible', 'N/A')} | Cilindraje: {veh.get('cilindraje', 'N/A')} cc</i>",
        f"<i>Estado: {veh.get('estadoVehiculo', 'N/A')}</i>",
        f"<i>Organismo: {lic.get('autoridadTransito', 'N/A')}</i>",
        f"<i>Matricula: {lic.get('fechaMatricula', 'N/A')}</i>",
        "",
        "<i>SOAT</i>",
        f"<i>Vigente: {'SI' if soat_vig.get('vigente') == 'SI' else 'NO'} | Entidad: {soat_vig.get('entidadEmisora', 'N/A')}</i>",
        f"<i>Hasta: {soat_vig.get('fechaFinVigencia', 'N/A')}</i>",
        "",
        "<i>REVISION TECNICO-MECANICA</i>",
        f"<i>Vigente: {'SI' if rtm_vig.get('vigente') == 'SI' else 'NO'} | CDA: {rtm_vig.get('cdaExpideRTM', 'N/A')}</i>",
        f"<i>Hasta: {rtm_vig.get('fechaVigencia', 'N/A')}</i>"
    ]
    return wrap_footer("\n".join(lines))


def format_simit(data: Dict, placa: str) -> str:
    lines = [
        f"<i>SIMIT - {placa}</i>\n",
        f"<i>Paz y Salvo: {'SI' if data.get('pazSalvo') else 'NO'}</i>",
        f"<i>Cancelada: {'SI' if data.get('cancelada') else 'NO'}</i>",
        f"<i>Suspendida: {'SI' if data.get('suspendida') else 'NO'}</i>",
        f"<i>Total General: ${data.get('totalGeneral', 0):,}</i>",
        f"<i>Total a Pagar: ${data.get('totalMultasPagar', 0):,}</i>",
        f"<i>Multas Pendientes: {data.get('cantMultasPagar', 0)}</i>",
        f"<i>Total Multas: {len(data.get('multas', []))}</i>",
        f"<i>Comparendos: {len(data.get('comparendos', []))}</i>",
    ]
    multas = data.get('multas', [])[:5]
    if multas:
        lines.append("\n<i>Ultimas multas:</i>")
        for m in multas:
            inf = m.get('infractor', {})
            lines.append(f"<i>    • Comparendo: {m.get('numeroComparendo', 'N/A')} | Valor: ${m.get('valorPagar', 0):,}</i>")
            lines.append(f"<i>      Estado: {m.get('estadoComparendo', 'N/A')} | Infractor: {inf.get('nombre', 'N/A')} {inf.get('apellido', '')}</i>")
    return wrap_footer("\n".join(lines))


# ============================================================================
# CAPTCHA SIMIT
# ============================================================================
def is_prime(value: int) -> bool:
    if value <= 1:
        return False
    for i in range(2, int(math.sqrt(value)) + 1):
        if value % i == 0:
            return False
    return True


def solve_captcha(question: str, time_val: int) -> Optional[Dict]:
    target = "0000"
    for nonce in range(2, 5_000_000):
        if not is_prime(nonce):
            continue
        payload = {"question": question, "time": time_val, "nonce": nonce}
        raw = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        if hashlib.sha256(raw.encode("utf-8")).hexdigest()[:4] == target:
            return payload
    return None


def get_captcha_question() -> Optional[Dict]:
    try:
        boundary = f"----WebKitFormBoundary{int(time.time() * 1000)}"
        body = f"--{boundary}\r\nContent-Disposition: form-data; name=\"endpoint\"\r\n\r\nquestion\r\n--{boundary}--\r\n"
        headers = {
            "Accept": "*/*",
            "Origin": "https://www.fcm.org.co",
            "Referer": "https://www.fcm.org.co/",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": HEADERS["User-Agent"]
        }
        r = requests.post(CAPTCHA_API, data=body.encode(), headers=headers, timeout=30)
        if r.status_code == 200:
            data = r.json()
            if not data.get("error"):
                return data.get("data")
    except Exception:
        pass
    return None


# ============================================================================
# COMANDOS GENERALES Y ADMIN
# ============================================================================
@bot.message_handler(commands=["start"])
def handle_start(message):
    texto = (
        "BOT DE CONSULTAS OSINT\n\n"
        "COMANDOS COLOMBIA\n"
        "• /runt PLACA\n• /runtfull PLACA\n• /simit PLACA\n"
        "• /cedula DOCUMENTO\n• /nequiCO CEDULA\n• /telCO CELULAR\n• /matricula PLACA\n\n"
        "COMANDOS ARGENTINA\n"
        "• /tel NUMERO (Movistar)\n• /informeBA DNI\n• /basico DNI\n• /ftdni DNI\n• /dnrpa PATENTE\n"
        "• /rto DOMINIO\n• /argfull DOMINIO\n\n"
        "CUENTA & ADMIN\n"
        "• /info\n• /dar ID CANTIDAD\n• /adm ID\n• /stats"
    )
    bot.reply_to(message, f"<blockquote><pre><i>{html.escape(texto)}</i></pre></blockquote>", parse_mode="HTML")


@bot.message_handler(commands=["tel"])
def handle_tel(message):
    if not verificar_y_descontar_tokens(message, 1):
        return
    args = message.text.split()
    if len(args) < 2:
        bot.reply_to(message, "<i>Ejemplo: /tel 1112345678</i>", parse_mode="HTML")
        return

    numero = args[1].strip()
    bot.reply_to(message, f"<i>Consultando Movistar para: <code>{numero}</code> (Esto puede tomar unos segundos)...</i>", parse_mode="HTML")

    resultado = ejecutar_consulta_movistar(numero)
    bot.reply_to(message, f"<blockquote>{format_response(resultado)}</blockquote>", parse_mode="HTML")


@bot.message_handler(commands=["dnrpa"])
def handle_dnrpa(message):
    if not verificar_y_descontar_tokens(message, 1):
        return
    args = message.text.split()
    if len(args) < 2:
        bot.reply_to(message, "<i>Ejemplo: /dnrpa AB123CD</i>", parse_mode="HTML")
        return

    patente = args[1].upper().strip()
    bot.reply_to(message, f"<i>Consultando DNRPA oficial para: {patente}...</i>", parse_mode="HTML")

    res = search_patente_dnrpa(patente)
    texto_formateado = format_dnrpa_secciones(res, patente)
    bot.reply_to(message, f"<blockquote>{texto_formateado}</blockquote>", parse_mode="HTML")


@bot.message_handler(commands=["rto"])
def handle_rto(message):
    if not verificar_y_descontar_tokens(message, 1):
        return

    args = message.text.split()
    if len(args) < 2:
        bot.reply_to(message, "<i>Ejemplo: /rto AB123CD</i>", parse_mode="HTML")
        return

    dominio = args[1].upper().strip()

    if not REGEX_DOMINIO_AR.match(dominio):
        bot.reply_to(
            message,
            "<i>Formato de dominio invalido. Ej: AAA123 o AA123BB</i>",
            parse_mode="HTML"
        )
        return

    bot.reply_to(
        message,
        f"<i>Consultando RTO para: <code>{dominio}</code>...</i>",
        parse_mode="HTML"
    )

    datos = consultar_rto(dominio)
    texto = format_rto(datos, dominio)
    bot.reply_to(message, f"<blockquote>{texto}</blockquote>", parse_mode="HTML")


@bot.message_handler(commands=["argfull"])
def handle_argfull(message):
    if not verificar_y_descontar_tokens(message, 2):
        return

    args = message.text.split()
    if len(args) < 2:
        bot.reply_to(message, "<i>Ejemplo: /argfull AB123CD</i>", parse_mode="HTML")
        return

    dominio = args[1].upper().strip()

    if not REGEX_DOMINIO_AR.match(dominio):
        bot.reply_to(message, "<i>Dominio invalido. Ej: AAA123 o AA123BB</i>", parse_mode="HTML")
        return

    bot.reply_to(
        message,
        f"<i>Consultando DNRPA + RTO para: <code>{dominio}</code>...</i>",
        parse_mode="HTML"
    )

    data_dnrpa = search_patente_dnrpa(dominio)
    if "error" in data_dnrpa:
        texto_dnrpa = f"<i>DNRPA Error: {html.escape(str(data_dnrpa['error']))}</i>"
    else:
        lines_d = [f"<i>INFORME DNRPA - {dominio}</i>\n", "<i>INFORMACION DEL VEHICULO</i>"]
        for k, v in data_dnrpa.items():
            if k.lower() != "dominio" and v:
                lines_d.append(f"<i>• <b>{str(k).replace('_', ' ').title()}:</b> {html.escape(str(v))}</i>")
        texto_dnrpa = "\n".join(lines_d)

    data_rto = consultar_rto(dominio)
    if data_rto.get("_estado") != "OK":
        texto_rto = f"<i>RTO - {dominio}</i>\n\n<i>{html.escape(data_rto.get('mensaje', 'Sin datos'))}</i>"
    else:
        texto_rto = (
            f"<i>RTO - {dominio}</i>\n\n"
            f"<i><b>Resultado:</b> {html.escape(data_rto.get('Resultado', '-'))}</i>\n"
            f"<i><b>Tipo:</b> {html.escape(data_rto.get('Tipo de revision', '-'))}</i>\n"
            f"<i><b>Revision:</b> {html.escape(data_rto.get('Fecha Revision', '-'))}</i>\n"
            f"<i><b>Vencimiento:</b> {html.escape(data_rto.get('Fecha Vencimiento', '-'))}</i>\n"
            f"<i><b>Certificado:</b> {html.escape(data_rto.get('Certificado', '-'))}</i>\n"
            f"<i><b>Centro:</b> {html.escape(data_rto.get('Centro de Revision Tecnica', '-'))}</i>"
        )

    resultado_unido = f"{texto_dnrpa}\n\n<i>--------------------</i>\n\n{texto_rto}"
    
    bot.reply_to(
        message,
        f"<blockquote>{wrap_footer(resultado_unido)}</blockquote>",
        parse_mode="HTML"
    )


@bot.message_handler(commands=["info"])
def handle_info(message):
    target = message.reply_to_message.from_user if message.reply_to_message else message.from_user
    uid = target.id
    tokens = obtener_tokens_usuarios().get(str(uid), {}).get("tokens", 0)
    admin = "Si" if uid in obtener_admins() else "No"
    txt = (
        "<i>INFORMACION DE CUENTA</i>\n\n"
        f"<i>ID: <code>{uid}</code></i>\n"
        f"<i>Nombre: {html.escape(target.first_name or '')}</i>\n"
        f"<i>Es Admin: {admin}</i>\n"
        f"<i>Tokens: <b>{tokens}</b></i>"
    )
    bot.reply_to(message, f"<blockquote>{txt}</blockquote>", parse_mode="HTML")


@bot.message_handler(commands=["dar"])
def handle_dar(message):
    if message.from_user.id not in obtener_admins():
        bot.reply_to(message, "<i>Sin permisos.</i>", parse_mode="HTML")
        return
    args = message.text.split()
    if len(args) < 3:
        bot.reply_to(message, "<i>Ejemplo: /dar 123456789 50</i>", parse_mode="HTML")
        return
    try:
        t_id, cant = args[1], int(args[2])
        if cant <= 0:
            return
        cur = obtener_tokens_usuarios().get(t_id, {}).get("tokens", 0) + cant
        guardar_token_db(t_id, cur, "admin")
        bot.reply_to(message, f"<i>Agregados {cant} tokens a {t_id}. Total: {cur}</i>", parse_mode="HTML")
    except ValueError:
        bot.reply_to(message, "<i>Parametro invalido.</i>", parse_mode="HTML")


@bot.message_handler(commands=["adm"])
def handle_adm(message):
    if message.from_user.id not in obtener_admins():
        bot.reply_to(message, "<i>Sin permisos.</i>", parse_mode="HTML")
        return
    args = message.text.split()
    if len(args) < 2:
        return
    try:
        new_id = int(args[1])
        agregar_admin_db(new_id)
        bot.reply_to(message, f"<i>Usuario {new_id} es admin.</i>", parse_mode="HTML")
    except ValueError:
        bot.reply_to(message, "<i>ID numerica requerida.</i>", parse_mode="HTML")


@bot.message_handler(commands=["stats"])
def handle_stats(message):
    if message.from_user.id not in obtener_admins():
        bot.reply_to(message, "<i>Sin permisos.</i>", parse_mode="HTML")
        return
    tokens = obtener_tokens_usuarios()
    t_str = "\n".join(f"<i>• <code>{uid}</code>: <b>{v['tokens']}</b></i>" for uid, v in tokens.items()) or "<i>Vacio</i>"
    bot.reply_to(message, f"<blockquote><i>Admin Stats:\nTokens:\n{t_str}</i></blockquote>", parse_mode="HTML")


# ============================================================================
# COMANDOS NUEVOS ARGENTINA (/basico y /ftdni)
# ============================================================================
@bot.message_handler(commands=["basico"])
def handle_basico(message):
    if not verificar_y_descontar_tokens(message, 1):
        return
    args = message.text.split()
    if len(args) < 2:
        bot.reply_to(message, "<i>Ejemplo: /basico 49114263</i>", parse_mode="HTML")
        return

    dni = args[1].strip()
    bot.reply_to(message, f"<i>Consultando datos básicos para DNI: <code>{dni}</code>...</i>", parse_mode="HTML")

    try:
        url = f"{CREDICUOTAS_API}{dni}"
        r = requests.get(url, headers=HEADERS, timeout=30)
        if r.status_code == 200:
            resultado = r.json()
            bot.reply_to(message, f"<blockquote>{format_response(resultado)}</blockquote>", parse_mode="HTML")
        else:
            bot.reply_to(message, wrap_footer("<i>No se encontró información o la API no respondió correctamente.</i>"), parse_mode="HTML")
    except Exception as e:
        bot.reply_to(message, wrap_footer(f"<i>Error: {str(e)}</i>"), parse_mode="HTML")


@bot.message_handler(commands=["ftdni"])
def handle_ftdni(message):
    if not verificar_y_descontar_tokens(message, 1):
        return
    args = message.text.split()
    if len(args) < 2:
        bot.reply_to(message, "<i>Ejemplo: /ftdni 49114263</i>", parse_mode="HTML")
        return

    dni = args[1].strip()
    bot.reply_to(message, f"<i>Buscando foto de DNI para: <code>{dni}</code>...</i>", parse_mode="HTML")

    try:
        r = requests.get(FOTODNI_API, params={"dni": dni}, headers=HEADERS, timeout=30)
        if r.status_code == 200:
            res_json = r.json()
            
            datos = res_json.get("datos", {})
            foto_base64 = res_json.get("foto", "")
            
            cuil = datos.get("cuil", "N/A")
            nro_dni = datos.get("dni", "N/A")
            fecha_nac = datos.get("fecha_nacimiento", "N/A")
            nombre = datos.get("nombrecompleto", "N/A")
            sexo = datos.get("sexo", "N/A")
            
            # Caption corto para respetar el límite estricto de Telegram en fotos (máx 1024 caracteres)
            caption = (
                f"<b>FOTO DNI - ARGENTINA</b>\n"
                f"• <b>Nombre:</b> {html.escape(str(nombre))}\n"
                f"• <b>DNI:</b> {html.escape(str(nro_dni))}\n"
                f"• <b>CUIL:</b> {html.escape(str(cuil))}\n"
                f"• <b>Nacimiento:</b> {html.escape(str(fecha_nac))}\n"
                f"• <b>Sexo:</b> {html.escape(str(sexo))}"
                f"{FOOTER_HIDDEN}"
            )
            
            if foto_base64:
                if "," in foto_base64:
                    foto_base64 = foto_base64.split(",")[1]
                
                foto_bytes = base64.b64decode(foto_base64)
                foto_io = BytesIO(foto_bytes)
                foto_io.name = f"dni_{dni}.jpg"
                
                bot.send_photo(
                    chat_id=message.chat.id,
                    photo=foto_io,
                    caption=caption,
                    parse_mode="HTML",
                    reply_to_message_id=message.message_id
                )
            else:
                bot.reply_to(message, wrap_footer("<i>Se encontraron los datos pero no la imagen asociada.</i>"), parse_mode="HTML")
        else:
            bot.reply_to(message, wrap_footer("<i>No se encontró información para este DNI.</i>"), parse_mode="HTML")
    except Exception as e:
        bot.reply_to(message, wrap_footer(f"<i>Error al procesar la foto: {str(e)}</i>"), parse_mode="HTML")


# ============================================================================
# COMANDOS COLOMBIA
# ============================================================================
@bot.message_handler(commands=["runt"])
def handle_runt(message):
    if not verificar_y_descontar_tokens(message, 1):
        return
    args = message.text.split()
    if len(args) < 2:
        return
    placa = args[1].upper().strip()
    try:
        r = requests.get(RUNT_API, params={"placa": placa}, headers=HEADERS, timeout=30)
        if r.status_code == 200 and r.json().get("caracteristicasVehiculo"):
            bot.reply_to(message, f"<blockquote>{format_runt(r.json())}</blockquote>", parse_mode="HTML")
        else:
            bot.reply_to(message, wrap_footer("<i>No encontrado</i>"), parse_mode="HTML")
    except Exception as e:
        bot.reply_to(message, wrap_footer(f"<i>Error: {str(e)}</i>"), parse_mode="HTML")


@bot.message_handler(commands=["runtfull"])
def handle_runtfull(message):
    if not verificar_y_descontar_tokens(message, 1):
        return
    args = message.text.split()
    if len(args) < 2:
        return
    placa = args[1].upper().strip()
    try:
        r = requests.get(RUNT_API, params={"placa": placa}, headers=HEADERS, timeout=30)
        js = json.dumps(r.json(), indent=2, ensure_ascii=False)[:3800]
        bot.reply_to(message, f"<blockquote><pre><i>{html.escape(js)}</i></pre>{FOOTER_HIDDEN}</blockquote>", parse_mode="HTML")
    except Exception as e:
        bot.reply_to(message, wrap_footer(f"<i>Error: {str(e)}</i>"), parse_mode="HTML")


@bot.message_handler(commands=["simit"])
def handle_simit(message):
    if not verificar_y_descontar_tokens(message, 1):
        return
    args = message.text.split()
    if len(args) < 2:
        return
    placa = args[1].upper().strip()
    try:
        cq = get_captcha_question()
        ans = solve_captcha(str(cq.get("question")), int(time.time())) if cq else None
        h_s = {**HEADERS, "Content-Type": "application/json",
               "Origin": "https://www.fcm.org.co", "Referer": "https://www.fcm.org.co/"}
        r = requests.post(SIMIT_API, json={
            "filtro": placa,
            "reCaptchaDTO": {"response": json.dumps([ans], separators=(",", ":")), "consumidor": "1"}
        }, headers=h_s, timeout=60)
        bot.reply_to(message, f"<blockquote>{format_simit(r.json(), placa)}</blockquote>", parse_mode="HTML")
    except Exception as e:
        bot.reply_to(message, wrap_footer(f"<i>Error: {str(e)}</i>"), parse_mode="HTML")


# ============================================================================
# INICIO DEL BOT
# ============================================================================
if __name__ == "__main__":
    print("Bot de Telegram OSINT iniciado correctamente...")
    bot.infinity_polling()
