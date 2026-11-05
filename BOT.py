import logging
import re
import io
import asyncio
import json
import urllib3
import base64
import requests
from pathlib import Path
from datetime import datetime
from bs4 import BeautifulSoup
from urllib.parse import urljoin
from playwright.async_api import async_playwright, TimeoutError as PWTimeout
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes
from telegram.constants import ParseMode

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

DUEÑO_ID = 8639936549  
ACCESOS_PERMANENTES = {DUEÑO_ID, 8639936549}
RENAPER_URL = "https://rena.addgroup.workers.dev/"
RENAPER_TOKEN = "rn_b1f18e4386b6416483e11f6602065ab7"
NOSIS_BASE_URL = "https://ws01.nosis.com/rest/variables"
NOSIS_USER = "448608"
NOSIS_TOKEN = "914286"
RTO_BASE_URL = "https://rto.cent.ar/rto"
RTO_LOGIN_URL = f"{RTO_BASE_URL}/j_spring_security_check"
RTO_USERNAME = "martinezgu"
RTO_PASSWORD = "artigas855"
RTO_MAX_IMAGENES = 5

# Configuración IIC Mendoza para Comercial (/comercial)
IIC_USUARIO = "7198"
IIC_CLAVE = "lowen2022"
IIC_BASE = "https://iic-mendoza.com.ar/iicmendoza/servlet/com.informe3"
PDF_DIR = Path("pdfs_temp")

def verificar_acceso(user_id):
    return user_id in ACCESOS_PERMANENTES

async def mensaje_acceso_denegado(update: Update):
    user_id = update.effective_user.id
    await update.message.reply_text(f"Acceso denegado.\nTu ID: {user_id}\nPedile al dueño que te autorice.")

async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    await update.message.reply_text(f"¡Hola! Bienvenido al bot de consultas.\nTu ID de Telegram es: {user_id}\n\nUsa /comandos para ver la lista de comandos disponibles.")

async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Tu ID de Telegram es: {update.effective_user.id}")

async def seller_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Informacion de Seller:\nID autorizado: 8639936549")

async def comandos_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    texto = (
        "Lista de comandos disponibles:\n\n"
        "/start - Inicia el bot y muestra tu ID\n"
        "/comandos - Muestra esta lista\n"
        "/id - Muestra tu ID de Telegram\n"
        "/renaper [DNI] [SEXO] - Consulta Renaper (Ej: /renaper 13371337 F)\n"
        "/nosis [DNI] - Consulta Nosis (Ej: /nosis 13371337)\n"
        "/comercial [DNI] - Consulta Comercial PDF (Ej: /comercial 33005699)\n"
        "/fotorto [PATENTE] - Consulta RTO y fotos (Ej: /fotorto GAY123)\n"
        "/seller - Muestra informacion del seller\n"
    )
    if update.effective_user.id == DUEÑO_ID:
        texto += "\n/dar [ID] - Da acceso permanente a un usuario"
    await update.message.reply_text(texto)

async def dar_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != DUEÑO_ID:
        await update.message.reply_text("No tienes permisos para usar este comando.")
        return
    if not context.args or not context.args[0].isdigit():
        await update.message.reply_text("Uso incorrecto. Ejemplo: /dar 123456789")
        return
    nuevo_id = int(context.args[0])
    ACCESOS_PERMANENTES.add(nuevo_id)
    await update.message.reply_text(f"Acceso permanente otorgado al ID: {nuevo_id}")

async def renaper_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not verificar_acceso(update.effective_user.id):
        await mensaje_acceso_denegado(update)
        return
    if len(context.args) < 2:
        await update.message.reply_text("Uso incorrecto.\nEjemplo de uso: /renaper 13371337 F")
        return
    dni, sexo = context.args[0].strip(), context.args[1].strip().upper()
    if not dni.isdigit() or sexo not in ["M", "F"]:
        await update.message.reply_text("DNI inválido o sexo incorrecto (debe ser M o F).")
        return
    loading = await update.message.reply_text(f"Consultando Renaper para DNI {dni}...")
    try:
        def consultar_api():
            url_endpoint = RENAPER_URL.rstrip("/") + "/api/renaper"
            headers = {"X-API-Key": RENAPER_TOKEN, "Authorization": f"Bearer {RENAPER_TOKEN}", "User-Agent": "Mozilla/5.0"}
            params = {"dni": dni, "sexo": sexo}
            resp = requests.get(url_endpoint, headers=headers, params=params, timeout=15)
            if resp.status_code == 404:
                resp = requests.get(RENAPER_URL, headers=headers, params=params, timeout=15)
            return resp
        response = await asyncio.to_thread(consultar_api)
        if response.status_code == 200:
            try:
                data = response.json()
                texto_respuesta = f"Resultado RENAPER:\n```json\n{json.dumps(data, indent=2, ensure_ascii=False)}\n```"
            except Exception:
                texto_respuesta = f"Resultado RENAPER:\n{response.text}"
            await loading.edit_text(texto_respuesta, parse_mode=ParseMode.MARKDOWN)
        else:
            await loading.edit_text(f"Error en la API de Renaper (Código {response.status_code}):\n{response.text[:500]}")
    except Exception as e:
        logger.error(f"Error en /renaper: {e}")
        await loading.edit_text(f"Ocurrió un error al consultar Renaper: {e}")

def procesar_resumen_nosis(data):
    """Extrae de forma limpia y sin emojis los valores clave de Nosis"""
    try:
        contenido = data.get("Contenido", {})
        
        if "Datos" in contenido and "Coincidencias" in contenido["Datos"]:
            coincidencias = contenido["Datos"]["Coincidencias"]
            res = "Documento Ambiguo (Varias coincidencias):\n\n"
            for c in coincidencias:
                res += f"- {c.get('RazonSocial')} (DNI: {c.get('Documento')}, Sexo: {c.get('Sexo')})\n"
            return res

        variables_dict = {}
        def extraer_vars(obj):
            if isinstance(obj, list):
                for item in obj:
                    if isinstance(item, dict):
                        if "Nombre" in item and "Valor" in item:
                            variables_dict[item["Nombre"]] = item["Valor"]
                        else:
                            extraer_vars(item)
            elif isinstance(obj, dict):
                for k, v in obj.items():
                    if k == "Variables" and isinstance(v, list):
                        for item in v:
                            if isinstance(item, dict) and "Nombre" in item and "Valor" in item:
                                variables_dict[item["Nombre"]] = item["Valor"]
                    if isinstance(v, (dict, list)):
                        extraer_vars(v)
                    elif k in ["RazonSocial", "Apellido", "Nombre", "CUIT", "Documento", "MontoTotal", "PeorSituacion", "EsMoroso", "CantidadBancos", "Calle", "Provincia"]:
                        if v is not None and v != "":
                            variables_dict[k] = v

        extraer_vars(contenido)

        nombre = (
            variables_dict.get("RazonSocial") or 
            variables_dict.get("VI_RazonSocial") or 
            f"{variables_dict.get('Apellido', '')} {variables_dict.get('Nombre', '')}".strip() or 
            variables_dict.get("VI_NombreApellido") or 
            "No especificado"
        )
        cuit = (
            variables_dict.get("CUIT") or 
            variables_dict.get("Documento") or 
            variables_dict.get("VI_CUIT") or 
            variables_dict.get("VI_Documento") or 
            "No disponible"
        )
        calle = variables_dict.get("Calle") or variables_dict.get("VI_DomAF_Calle") or "No especificada"
        localidad = variables_dict.get("Localidad") or variables_dict.get("VI_DomAF_Localidad") or ""
        provincia = variables_dict.get("Provincia") or variables_dict.get("VI_DomAF_Provincia") or "No especificada"
        domicilio = f"{calle}, {localidad} {provincia}".strip(", ")
        
        monto = variables_dict.get("MontoTotal") or variables_dict.get("VI_MontoTotal") or "0"
        situacion = variables_dict.get("PeorSituacion") or variables_dict.get("VI_PeorSituacion") or "1"
        moroso = variables_dict.get("EsMoroso") or variables_dict.get("VI_EsMoroso") or "No"
        bancos = variables_dict.get("CantidadBancos") or variables_dict.get("VI_CantidadBancos") or "0"

        resumen = (
            "Resumen Financiero / Nosis\n\n"
            f"Nombre: {nombre}\n"
            f"CUIT / Documento: {cuit}\n"
            f"Domicilio: {domicilio}\n\n"
            f"Monto Total Adeudado: ${monto}\n"
            f"Peor Situacion Crediticia: {situacion}\n"
            f"Es Moroso?: {moroso}\n"
            f"Cantidad de Bancos: {bancos}\n"
        )
        return resumen
    except Exception:
        return "Resumen generado. Ver archivo adjunto."

async def nosis_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not verificar_acceso(update.effective_user.id):
        await mensaje_acceso_denegado(update)
        return
    if not context.args or not context.args[0].strip().isdigit():
        await update.message.reply_text("Uso incorrecto.\nEjemplo de uso: /nosis 13371337")
        return
    dni = context.args[0].strip()
    loading = await update.message.reply_text(f"Consultando Nosis para documento {dni}...")
    url = f"{NOSIS_BASE_URL}?usuario={NOSIS_USER}&token={NOSIS_TOKEN}&documento={dni}&VR=2&format=json"
    try:
        response = await asyncio.to_thread(lambda: requests.get(url, timeout=15))
        if response.status_code in (200, 422):
            try:
                data = response.json()
                json_str = json.dumps(data, indent=2, ensure_ascii=False)
                
                resumen_texto = procesar_resumen_nosis(data)
                await loading.edit_text(resumen_texto)
                
                txt_bytes = io.BytesIO(json_str.encode("utf-8"))
                txt_bytes.name = f"nosis_{dni}.txt"
                txt_bytes.seek(0)
                
                await update.message.reply_document(
                    document=txt_bytes,
                    caption=f"Archivo TXT completo de Nosis para el documento {dni}"
                )
            except Exception as ex:
                await loading.edit_text(f"Error procesando la respuesta de Nosis: {ex}")
        else:
            await loading.edit_text(f"Error en la API de Nosis (Código {response.status_code}):\n{response.text[:500]}")
    except Exception as e:
        logger.error(f"Error en /nosis: {e}")
        await loading.edit_text(f"Ocurrió un error al consultar Nosis: {e}")

async def comercial_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not verificar_acceso(update.effective_user.id):
        await mensaje_acceso_denegado(update)
        return
    if not context.args:
        await update.message.reply_text(
            "Formato incorrecto\n\nUso correcto:\n/comercial DNI\n\nEjemplo:\n/comercial 33005699"
        )
        return
    
    dni = context.args[0].strip()
    if not dni.isdigit() or not (7 <= len(dni) <= 11):
        await update.message.reply_text(
            "Formato incorrecto\n\nUso correcto:\n/comercial DNI\n\nEjemplo:\n/comercial 33005699\n\nEl DNI debe contener solo dígitos (7 a 11 dígitos)."
        )
        return

    mensaje_busqueda = await update.message.reply_text(
        "Consultando en INFORME\naguarde unos minutos..."
    )
    
    PDF_DIR.mkdir(exist_ok=True)
    
    try:
        resultado = await _consultar_iic_dni(dni, mensaje_busqueda)
        
        if resultado:
            await mensaje_busqueda.edit_text(
                f"Exito / Consulta confirmada\n\n"
                f"Datos del informe:\n\n"
                f"CUIL/CUIT: {resultado['cuil']}\n"
                f"Nombre: {resultado['nombre']}\n"
                f"Documento: {resultado['documento']}\n"
                f"Ultima Consulta: {resultado['ultima_consulta']}\n\n"
                f"PDF generado exitosamente"
            )
            
            pdf_path = Path(resultado['archivo'])
            if pdf_path.exists():
                with open(pdf_path, 'rb') as f:
                    await update.message.reply_document(
                        document=f,
                        filename=pdf_path.name,
                        caption=f"Informe Comercial - DNI: {dni}"
                    )
                try:
                    pdf_path.unlink()
                except:
                    pass
        else:
            await mensaje_busqueda.edit_text("Error en la consulta Comercial - No se pudieron obtener resultados.")
            
    except Exception as e:
        logger.error(f"Error en comercial_command: {e}")
        await mensaje_busqueda.edit_text("Error en la consulta Comercial - Ocurrió un error inesperado.")

async def _consultar_iic_dni(dni: str, mensaje_busqueda) -> dict:
    """Consulta el DNI en IIC Mendoza usando Playwright[cite: 8] y descarga el PDF"""
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=False)
        ctx = await browser.new_context(accept_downloads=True)
        page = await ctx.new_page()
        
        try:
            logger.info("Abriendo login IIC Mendoza...")
            await page.goto(f"{IIC_BASE}.login", timeout=60000)
            await page.fill("#vUSUARIOID", IIC_USUARIO)
            await page.fill("#vUSUARIOPASSWORDPWS", IIC_CLAVE)
            await page.click("#ENTER")
            await page.wait_for_url("**/com.informe3.homevalidaciones", timeout=60000)
            logger.info("Login OK")
            
            await mensaje_busqueda.edit_text("Consultando en INFORME\naguarde unos minutos..")
            
            await page.click("#IMGCOMPANY")
            await page.wait_for_url("**/com.informe3.wwpersonanuevavalidaciones", timeout=60000)
            
            await mensaje_busqueda.edit_text("Consultando en INFORME\nBuscando DNI...")
            
            await page.fill("#vPERSONADOCUMENTOCHR", dni)
            await page.dispatch_event("#vPERSONADOCUMENTOCHR", "change")
            await page.click("#BTNTOGGLE")
            
            try:
                await page.wait_for_selector("#GridContainerRow_0001", timeout=30000)
            except PWTimeout:
                logger.error(f"No se encontraron resultados para DNI {dni}")
                await page.screenshot(path="error_iic.png")
                return None
            
            fila = await page.query_selector("#GridContainerRow_0001")
            
            cuil_elem = await fila.query_selector("[id^='span_vCUIL_']")
            nombre_elem = await fila.query_selector("[id^='span_vNOMBRE_']")
            documento_elem = await fila.query_selector("[id^='span_vDOCUMENTO_']")
            ult_cons_elem = await fila.query_selector("[id^='span_vPERSONAULTIMACONSULTA_']")
            
            cuil = (await cuil_elem.inner_text()).strip() if cuil_elem else 'N/A'
            nombre = (await nombre_elem.inner_text()).strip() if nombre_elem else 'N/A'
            documento = (await documento_elem.inner_text()).strip() if documento_elem else 'N/A'
            ult_cons = (await ult_cons_elem.inner_text()).strip() if ult_cons_elem else 'N/A'
            
            await mensaje_busqueda.edit_text("Consultando en INFORME\nGenerando informe PDF...")
            
            async with page.expect_response(re.compile(r".*ainformepdf.*"), timeout=60000) as resp_info:
                await page.click("#vCOMERCIALTXT_0001")
            
            response = await resp_info.value
            pdf_url = response.url
            
            r = await ctx.request.get(pdf_url)
            if r.status != 200:
                logger.error(f"Error al descargar PDF (HTTP {r.status})")
                return None
            
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            nombre_archivo = PDF_DIR / f"informe_{documento}_{timestamp}.pdf"
            
            pdf_body = await r.body()
            with open(nombre_archivo, "wb") as f:
                f.write(pdf_body)
            
            return {
                "cuil": cuil,
                "nombre": nombre,
                "documento": documento,
                "ultima_consulta": ult_cons,
                "archivo": str(nombre_archivo),
            }
            
        except Exception as e:
            logger.error(f"Error en _consultar_iic_dni: {e}")
            try:
                await page.screenshot(path="error_excepcion.png")
            except:
                pass
            return None
        finally:
            await browser.close()

def rto_login():
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0", "Accept-Language": "es-AR,es;q=0.9"})
    resp = session.post(RTO_LOGIN_URL, data={"j_username": RTO_USERNAME, "j_password": RTO_PASSWORD}, allow_redirects=True, verify=False, timeout=12)
    if resp.status_code not in (200, 302):
        raise Exception(f"Error de login HTTP {resp.status_code}")
    return session

def rto_buscar_historico(session, dominio):
    url = f"{RTO_BASE_URL}/RTO/buscarHistorico"
    resp = session.post(url, data={"dominio": dominio, "limite": "10", "codigoPais": "AR"}, verify=False, timeout=12)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    revisiones, vistos = [], set()
    for button in soup.find_all("input", onclick=re.compile(r"verDetallePlanilla")):
        match = re.search(r"verDetallePlanilla\(\s*'([^']+)'\s*,\s*'([^']+)'\s*,\s*'([^']+)'\s*,\s*'([^']+)'\s*\)", button.get("onclick", ""))
        if match:
            clave = tuple(match.groups())
            if clave not in vistos:
                vistos.add(clave)
                revisiones.append({"idPlanilla": clave[0], "nroPlanilla": clave[1], "codTaller": clave[2], "versionRTO": clave[3]})
    return revisiones

def rto_extraer_imagenes_html(html_text):
    imagenes = []
    if not html_text:
        return imagenes
    soup = BeautifulSoup(html_text, "html.parser")
    for img in soup.find_all("img"):
        for src in [img.get("src"), img.get("data-src"), img.get("data-image"), img.get("data-original")]:
            if src:
                src = src.strip()
                if src.startswith("data:image/"):
                    try:
                        header, content = src.split(",", 1)
                        ext = "png" if "png" in header else ("webp" if "webp" in header else "jpg")
                        raw = base64.b64decode(content)
                        if raw:
                            imagenes.append({"tipo": "bytes", "datos": raw, "extension": ext})
                    except Exception:
                        pass
                else:
                    url = src if src.startswith(("http://", "https://")) else urljoin("https://rto.cent.gov.ar/", src)
                    imagenes.append({"tipo": "url", "url": url})
    return imagenes

def rto_ver_detalle(session, dominio, idPlanilla, nroPlanilla, codTaller, versionRTO):
    url = f"{RTO_BASE_URL}/RTO/verDetallePlanillaRevision"
    resp = session.post(url, data={"idPlanilla": idPlanilla, "dominio": dominio, "codigoPais": "AR", "nroPlanilla": nroPlanilla, "codTaller": codTaller, "versionRTO": versionRTO}, verify=False, timeout=12)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")
    resultado = {"dominio": dominio, "idPlanilla": idPlanilla, "detalle": {}, "imagenes": []}
    for div in soup.find_all("div", class_="InputBr"):
        label, strong = div.find("label"), div.find("strong")
        if label and strong:
            k, v = label.get_text(" ", strip=True).replace(":", ""), strong.get_text(" ", strip=True)
            if k and v:
                resultado["detalle"][k] = v
    try:
        foto_resp = session.post(f"{RTO_BASE_URL}/fotovalidacion/mostrar2FotoAjax", data={"idPlanilla": idPlanilla, "versionRTO": versionRTO, "contentPDF": "false"}, verify=False, timeout=12)
        if foto_resp.status_code == 200:
            resultado["imagenes"] = rto_extraer_imagenes_html(foto_resp.text)
    except Exception:
        pass
    return resultado

def rto_consultar(patente):
    session = rto_login()
    try:
        revisiones = rto_buscar_historico(session, patente)
        if not revisiones:
            return session, []
        resultados, claves = [], set()
        for r in revisiones:
            res = rto_ver_detalle(session, patente, r["idPlanilla"], r["nroPlanilla"], r["codTaller"], r["versionRTO"])
            clave = (res["idPlanilla"], r["nroPlanilla"], r["versionRTO"])
            if clave not in claves:
                claves.add(clave)
                resultados.append(res)
        return session, resultados
    except Exception:
        session.close()
        raise

async def fotorto_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not verificar_acceso(update.effective_user.id):
        await mensaje_acceso_denegado(update)
        return
    if not context.args:
        await update.message.reply_text("Uso:\n/fotorto PATENTE\n\nEjemplo:\n/fotorto GAY123")
        return
    patente = context.args[0].strip().upper()
    if not re.fullmatch(r"[A-Z0-9]{6,7}", patente):
        await update.message.reply_text("Patente inválida. Debe tener entre 6 y 7 caracteres.")
        return
    loading = await update.message.reply_text(f"Consultando RTO y fotos para {patente}...")
    session = None
    try:
        session, resultados = await asyncio.to_thread(rto_consultar, patente)
        if not resultados:
            await loading.edit_text(f"No se encontraron revisiones para {patente}.")
            return
        try:
            await loading.delete()
        except Exception:
            pass
        fotos_enviadas = 0
        for numero, resultado in enumerate(resultados, 1):
            texto = f"Detalle RTO #{numero}\nPatente: {patente}\n\nDatos de la revision\n\n"
            for k, v in resultado["detalle"].items():
                if v:
                    texto += f"{k}: {v}\n"
            await update.message.reply_text(texto)
            for img in resultado.get("imagenes", []):
                if fotos_enviadas >= RTO_MAX_IMAGENES:
                    break
                try:
                    content = img["datos"] if img["tipo"] == "bytes" else requests.get(img["url"], verify=False, timeout=10).content
                    if content:
                        foto = io.BytesIO(content)
                        foto.name = f"rto_{patente}_{fotos_enviadas + 1}.jpg"
                        foto.seek(0)
                        await update.message.reply_photo(photo=foto, caption=f"RTO #{numero} - {patente}")
                        fotos_enviadas += 1
                except Exception as e:
                    logger.warning(f"Error enviando foto: {e}")
        await update.message.reply_text(f"Consulta Fotorto finalizada\nPatente: {patente}\nFotos enviadas: {fotos_enviadas}")
    except Exception as e:
        logger.exception(f"Error en /fotorto {patente}")
        try:
            await loading.edit_text(f"Error al consultar RTO:\n{str(e)[:1000]}")
        except Exception:
            pass
    finally:
        if session:
            try: session.close()
            except Exception: pass

if __name__ == "__main__":
    TOKEN_TELEGRAM = "9040754822:AAHAgvC0wDJRy5_OjnG9N58nNao7frldWEI"
    app = ApplicationBuilder().token(TOKEN_TELEGRAM).build()
    app.add_handler(CommandHandler("start", start_command))
    app.add_handler(CommandHandler("comandos", comandos_command))
    app.add_handler(CommandHandler("id", id_command))
    app.add_handler(CommandHandler("dar", dar_command))
    app.add_handler(CommandHandler("seller", seller_command))
    app.add_handler(CommandHandler("renaper", renaper_command))
    app.add_handler(CommandHandler("nosis", nosis_command))
    app.add_handler(CommandHandler("comercial", comercial_command))
    app.add_handler(CommandHandler("fotorto", fotorto_command))
    print("Bot iniciado correctamente. Esperando comandos...")
    app.run_polling()