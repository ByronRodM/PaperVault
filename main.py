import hashlib
import json
import logging
import os
import re
import secrets
import shutil
import time
import uuid
from collections import deque
from datetime import datetime
from typing import List, Optional

from fastapi import FastAPI, UploadFile, File, Depends, HTTPException, Request, Form
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from PIL import Image
from pydantic import BaseModel
from sqlalchemy import func
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from core.database import init_db, get_db, Documento, PlantillaOCR, ZonaOCR, Usuario
from core.ocr_engine import procesar_documento, procesar_por_zonas, verificar_paddle, FORMATOS_SOPORTADOS, FORMATOS_IMAGEN

UPLOAD_DIR = "uploads"
REFERENCIAS_DIR = os.path.join(UPLOAD_DIR, "referencias")
TMP_DIR = os.path.join(UPLOAD_DIR, "tmp")
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs(REFERENCIAS_DIR, exist_ok=True)
os.makedirs(TMP_DIR, exist_ok=True)
os.makedirs("static", exist_ok=True)

# Cache en memoria: temp_key → {resultados, archivo_nombre, idioma, plantilla_id}
_preview_cache: dict = {}


# ── Registro de eventos de seguridad (Mejora H-14) ─────────
# Registro persistente de eventos de seguridad (login, altas/bajas, cambios de
# contraseña, descargas y borrados) en logs/seguridad.log.
os.makedirs("logs", exist_ok=True)
seguridad_log = logging.getLogger("papervault.seguridad")
if not seguridad_log.handlers:
    seguridad_log.setLevel(logging.INFO)
    _fh = logging.FileHandler(os.path.join("logs", "seguridad.log"), encoding="utf-8")
    _fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    seguridad_log.addHandler(_fh)


def _limpio(valor) -> str:
    """Neutraliza caracteres de control para que un valor no pueda forjar líneas del log."""
    return re.sub(r"[\x00-\x1f\x7f]", "?", str(valor))[:200]


def log_seguridad(evento: str, request: Optional[Request] = None, usuario: Optional[str] = None, extra: str = ""):
    ip = request.client.host if request is not None and request.client else "-"
    seguridad_log.info(
        "evento=%s usuario=%s ip=%s %s", evento, _limpio(usuario or "-"), ip, _limpio(extra)
    )


# ── Límite de intentos de inicio de sesión (Mejora H-05) ───
# Ventana deslizante en memoria por (IP, usuario). Tras 5 fallos en 5 minutos
# se bloquea temporalmente con respuesta 429.
_INTENTOS: dict = {}
_MAX_INTENTOS = 5
_VENTANA_SEG = 300
_MAX_CLAVES = 10_000  # por encima de esto se purgan las entradas vencidas


def _clave_intentos(request: Request, username: str) -> tuple:
    ip = request.client.host if request.client else "-"
    return (ip, username.lower())


def _vigentes(clave, ahora: float) -> Optional[deque]:
    q = _INTENTOS.get(clave)
    if q is None:
        return None
    while q and ahora - q[0] > _VENTANA_SEG:
        q.popleft()
    if not q:
        del _INTENTOS[clave]
        return None
    return q


def _esta_bloqueado(clave) -> bool:
    q = _vigentes(clave, time.time())
    return q is not None and len(q) >= _MAX_INTENTOS


def _registrar_fallo(clave):
    ahora = time.time()
    if len(_INTENTOS) >= _MAX_CLAVES:
        for k in list(_INTENTOS):
            _vigentes(k, ahora)
    _INTENTOS.setdefault(clave, deque()).append(ahora)


def _limpiar_intentos(clave):
    _INTENTOS.pop(clave, None)


def _json_seguro(obj) -> str:
    """
    Serializa a JSON para incrustar dentro de <script> sin permitir ruptura de
    contexto (Mejora H-04): se escapan < > & y separadores de línea Unicode.
    """
    s = json.dumps(obj, ensure_ascii=False)
    return (s.replace("<", "\\u003c").replace(">", "\\u003e").replace("&", "\\u0026")
             .replace("\u2028", "\\u2028").replace("\u2029", "\\u2029"))


# ── Validación de archivos subidos (Mejora H-11) ───────────
# Registro técnico: el detalle de los errores internos va aquí, no al cliente.
app_log = logging.getLogger("papervault")

# Firmas (cabeceras mágicas) aceptadas para cada extensión soportada.
_FIRMAS = {
    ".pdf":  (b"%PDF-",),
    ".png":  (b"\x89PNG\r\n\x1a\n",),
    ".jpg":  (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".tif":  (b"II*\x00", b"MM\x00*"),
    ".tiff": (b"II*\x00", b"MM\x00*"),
    ".bmp":  (b"BM",),
}


def _nombre_seguro(nombre: Optional[str]) -> str:
    """
    Deja solo el nombre base del archivo, sin rutas ni caracteres especiales.
    Evita que un nombre como "../../main.py" escriba fuera de uploads/.
    """
    base = os.path.basename((nombre or "").replace("\\", "/"))
    base = re.sub(r"[^\w.\-() ]", "_", base).strip(" .")
    return base[-150:] or "archivo"


def _validar_contenido(archivo: UploadFile, extension: str):
    """Comprueba que el contenido real del archivo corresponda a su extensión."""
    cabecera = archivo.file.read(16)
    archivo.file.seek(0)
    if not any(cabecera.startswith(f) for f in _FIRMAS.get(extension, ())):
        raise HTTPException(status_code=400, detail="El contenido del archivo no corresponde a su extensión.")


# ── Utilidades de contraseñas ──────────────────────────────

def _hashear(password: str) -> str:
    """PBKDF2-SHA256 con salt aleatorio. Formato: salt_hex:key_hex"""
    salt = secrets.token_hex(32)
    key = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 260_000).hex()
    return f"{salt}:{key}"


def _verificar(password: str, stored: str) -> bool:
    try:
        salt, key = stored.split(":", 1)
        nuevo = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 260_000).hex()
        return secrets.compare_digest(nuevo, key)
    except Exception:
        return False


# Hash con el mismo formato que _hashear, para igualar tiempos en logins de usuario inexistente (H-12)
_HASH_FICTICIO = _hashear(secrets.token_urlsafe(16))

app = FastAPI(
    title="PaperVault — Sistema de Gestión Documental",
    description="Sistema para escanear, procesar y gestionar documentos mediante OCR",
    version="1.0.0",
    # Mejora H-11: la documentación automática no es pública; se sirve más
    # abajo por rutas que exigen rol de administrador.
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

# ── Clave de sesión (Mejora de auditoría H-01) ─────────────
# La clave que firma las cookies de sesión DEBE provenir de una variable de
# entorno/secreto; ya no está escrita en el código. Sin SESSION_SECRET la
# aplicación no arranca, para evitar volver a una clave conocida por defecto.
SESSION_SECRET = os.getenv("SESSION_SECRET", "").strip()
if len(SESSION_SECRET) < 32:
    raise RuntimeError(
        "SESSION_SECRET no definida o demasiado corta (mínimo 32 caracteres). "
        "Genere una con: python -c \"import secrets; print(secrets.token_urlsafe(48))\" "
        "y colóquela en el archivo .env antes de iniciar la aplicación."
    )
SESSION_HTTPS = os.getenv("SESSION_HTTPS", "0") == "1"
# Cookie de sesión endurecida: HttpOnly (por defecto), SameSite=strict,
# caducidad de 30 minutos y Secure cuando se sirva por HTTPS (SESSION_HTTPS=1).
app.add_middleware(
    SessionMiddleware,
    secret_key=SESSION_SECRET,
    same_site="strict",
    https_only=SESSION_HTTPS,
    max_age=1800,
)
app.mount("/static", StaticFiles(directory="static"), name="static")
# Nota de auditoría (H-03): la carpeta uploads/ ya NO se publica como estática.
# Sus archivos se entregan por la ruta autenticada /uploads/{ruta} (ver más abajo).
templates = Jinja2Templates(directory="templates")


# ── Cabeceras de seguridad (Mejora de auditoría H-08) ──────
# Chart.js, bootstrap-icons e Inter se sirven desde /static/vendor, así que la CSP
# solo admite el propio origen. Se permite el enmarcado desde el mismo origen
# porque el visor muestra los PDF en un <iframe> apuntando a /ver/{id}.
CSP = "; ".join([
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data:",
    "frame-ancestors 'self'",
    "base-uri 'self'",
    "form-action 'self'",
])

# Swagger UI y ReDoc (solo admin) cargan sus recursos desde jsDelivr; ReDoc además
# usa Google Fonts y un worker blob:. Esta CSP se aplica únicamente a /docs y /redoc.
CSP_DOCS = "; ".join([
    "default-src 'self'",
    "script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net",
    "style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://fonts.googleapis.com",
    "font-src 'self' https://fonts.gstatic.com",
    "img-src 'self' data: https://cdn.redoc.ly",
    "worker-src 'self' blob:",
    "frame-ancestors 'none'",
    "base-uri 'self'",
    "form-action 'self'",
])


@app.middleware("http")
async def cabeceras_seguridad(request: Request, call_next):
    response = await call_next(request)
    response.headers.setdefault("Content-Security-Policy", CSP)
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "SAMEORIGIN")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    response.headers.setdefault("Permissions-Policy", "geolocation=(), microphone=(), camera=()")
    if SESSION_HTTPS:
        response.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
    return response


# ── Pydantic models ───────────────────────────────────────

class ZonaIn(BaseModel):
    nombre_campo: str
    x: int
    y: int
    ancho: int
    alto: int
    orden: int = 0


class ZonasPayload(BaseModel):
    zonas: List[ZonaIn]


# ── Helpers de sesión ─────────────────────────────────────

def get_user(request: Request) -> Optional[str]:
    return request.session.get("user")

def get_rol(request: Request) -> Optional[str]:
    return request.session.get("rol")

def usuario_valido(request: Request, db: Session) -> Optional[Usuario]:
    """
    Revalida la sesión contra la base de datos en cada petición (Mejora H-06):
    el usuario debe existir y seguir activo. Si fue eliminado o desactivado, la
    sesión se invalida de inmediato. El rol se toma de la base, no de la cookie.
    """
    nombre = get_user(request)
    if not nombre:
        return None
    u = db.query(Usuario).filter(Usuario.nombre_usuario == nombre, Usuario.activo == True).first()
    if not u:
        request.session.clear()
        return None
    request.session["rol"] = u.rol
    # Contraseña temporal pendiente (Mejora H-07): mientras primer_login sea
    # verdadero solo se permite cambiarla; el panel redirige y el resto responde 403.
    if u.primer_login and request.url.path != "/cambiar-password":
        if request.method == "GET" and request.url.path == "/":
            raise HTTPException(status_code=303, headers={"Location": "/cambiar-password"})
        raise HTTPException(status_code=403, detail="Debe cambiar la contraseña temporal antes de continuar.")
    return u


def require_sesion(request: Request, db: Session) -> Usuario:
    u = usuario_valido(request, db)
    if not u:
        raise HTTPException(status_code=401, detail="No autenticado")
    return u


def require_admin(request: Request, db: Session) -> Usuario:
    u = require_sesion(request, db)
    if u.rol != "admin":
        raise HTTPException(status_code=403, detail="Se requiere rol de administrador.")
    return u


@app.on_event("startup")
def startup():
    init_db()
    print("Base de datos inicializada.")
    db = next(get_db())
    try:
        if db.query(Usuario).count() == 0:
            password_inicial = secrets.token_urlsafe(10)
            admin = Usuario(
                nombre_usuario="admin",
                password_hash=_hashear(password_inicial),
                rol="admin",
                primer_login=True,
            )
            db.add(admin)
            db.commit()
            linea = "=" * 52
            print(f"\n{linea}")
            print("  PRIMER INICIO — Credenciales de administrador")
            print(f"  Usuario   : admin")
            print(f"  Contraseña: {password_inicial}")
            print(f"  Se pedirá cambiar la contraseña al ingresar.")
            print(f"{linea}\n")
    finally:
        db.close()
    if verificar_paddle():
        print("Motor OCR (PaddleOCR) listo.")
    else:
        print("ADVERTENCIA: PaddleOCR no está disponible.")


# ── Autenticación ─────────────────────────────────────────

@app.get("/login", response_class=HTMLResponse)
def pagina_login(request: Request):
    if get_user(request):
        return RedirectResponse(url="/", status_code=302)
    return templates.TemplateResponse("login.html", {"request": request, "error": None})


@app.post("/login")
async def hacer_login(
    request: Request,
    username: str = Form(...),
    password: str = Form(...),
    db: Session = Depends(get_db),
):
    clave = _clave_intentos(request, username)
    if _esta_bloqueado(clave):
        log_seguridad("login_bloqueado", request, username)
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Demasiados intentos fallidos. Espere unos minutos e intente de nuevo."},
            status_code=429,
        )
    usuario = db.query(Usuario).filter(
        Usuario.nombre_usuario == username,
        Usuario.activo == True,
    ).first()
    if usuario is None:
        # Cálculo PBKDF2 ficticio para igualar el tiempo de respuesta y evitar
        # la enumeración de usuarios por tiempo (Mejora H-12).
        _verificar(password, _HASH_FICTICIO)
    if not usuario or not _verificar(password, usuario.password_hash):
        _registrar_fallo(clave)
        log_seguridad("login_fallido", request, username)
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Usuario o contraseña incorrectos."},
            status_code=401,
        )
    _limpiar_intentos(clave)
    log_seguridad("login_ok", request, usuario.nombre_usuario)
    request.session["user"] = usuario.nombre_usuario
    request.session["rol"]  = usuario.rol
    if usuario.primer_login:
        return RedirectResponse(url="/cambiar-password", status_code=303)
    return RedirectResponse(url="/", status_code=303)


@app.get("/logout")
def hacer_logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=302)


@app.get("/cambiar-password", response_class=HTMLResponse)
def cambiar_password_get(request: Request, db: Session = Depends(get_db)):
    if not usuario_valido(request, db):
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("cambiar_password.html", {"request": request, "error": None})


@app.post("/cambiar-password")
async def cambiar_password_post(
    request: Request,
    password_nuevo: str = Form(...),
    password_confirmar: str = Form(...),
    db: Session = Depends(get_db),
):
    usuario = usuario_valido(request, db)
    if not usuario:
        return RedirectResponse(url="/login", status_code=302)
    if password_nuevo != password_confirmar:
        return templates.TemplateResponse(
            "cambiar_password.html",
            {"request": request, "error": "Las contraseñas no coinciden."},
            status_code=400,
        )
    if len(password_nuevo) < 8:
        return templates.TemplateResponse(
            "cambiar_password.html",
            {"request": request, "error": "La contraseña debe tener al menos 8 caracteres."},
            status_code=400,
        )
    usuario.password_hash = _hashear(password_nuevo)
    usuario.primer_login = False
    db.commit()
    log_seguridad("password_cambiado", request, usuario.nombre_usuario)
    return RedirectResponse(url="/", status_code=303)


# ── API: Gestión de usuarios (solo admin) ─────────────────

@app.get("/api/usuarios")
def listar_usuarios(request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    return [u.to_dict() for u in db.query(Usuario).order_by(Usuario.fecha_creacion).all()]


@app.post("/api/usuarios")
async def crear_usuario(
    request: Request,
    nombre_usuario: str = Form(...),
    rol: str = Form(default="usuario"),
    db: Session = Depends(get_db),
):
    require_admin(request, db)
    if db.query(Usuario).filter(Usuario.nombre_usuario == nombre_usuario).first():
        raise HTTPException(status_code=400, detail="El nombre de usuario ya existe.")
    if rol not in ("admin", "usuario"):
        raise HTTPException(status_code=400, detail="Rol inválido.")
    password_inicial = secrets.token_urlsafe(10)
    nuevo = Usuario(
        nombre_usuario=nombre_usuario,
        password_hash=_hashear(password_inicial),
        rol=rol,
        primer_login=True,
    )
    db.add(nuevo)
    db.commit()
    db.refresh(nuevo)
    log_seguridad("usuario_creado", request, get_user(request), f"nuevo={nombre_usuario} rol={rol}")
    data = nuevo.to_dict()
    data["password_inicial"] = password_inicial   # solo se devuelve una vez
    return data


@app.delete("/api/usuarios/{usuario_id}")
def eliminar_usuario(request: Request, usuario_id: int, db: Session = Depends(get_db)):
    require_admin(request, db)
    u = db.query(Usuario).filter(Usuario.id == usuario_id).first()
    if not u:
        raise HTTPException(status_code=404, detail="Usuario no encontrado.")
    if u.nombre_usuario == "admin":
        raise HTTPException(status_code=400, detail="No se puede eliminar al administrador principal.")
    db.delete(u)
    db.commit()
    log_seguridad("usuario_eliminado", request, get_user(request), f"objetivo={u.nombre_usuario}")
    return {"ok": True}


@app.post("/api/usuarios/{usuario_id}/reset")
def resetear_password(request: Request, usuario_id: int, db: Session = Depends(get_db)):
    require_admin(request, db)
    u = db.query(Usuario).filter(Usuario.id == usuario_id).first()
    if not u:
        raise HTTPException(status_code=404, detail="Usuario no encontrado.")
    nueva = secrets.token_urlsafe(10)
    u.password_hash = _hashear(nueva)
    u.primer_login = True
    db.commit()
    log_seguridad("password_reseteado", request, get_user(request), f"objetivo={u.nombre_usuario}")
    return {"password_temporal": nueva}


# ── Página principal (Dashboard) ──────────────────────────

@app.get("/", response_class=HTMLResponse)
def pagina_principal(request: Request, db: Session = Depends(get_db)):
    u = usuario_valido(request, db)
    if not u:
        return RedirectResponse(url="/login", status_code=302)
    user = u.nombre_usuario

    documentos =db.query(Documento).order_by(Documento.fecha_carga.desc()).all()
    plantillas = db.query(PlantillaOCR).order_by(PlantillaOCR.fecha_creacion.desc()).all()

    total     = db.query(func.count(Documento.id)).scalar() or 0
    total_pdf = db.query(func.count(Documento.id)).filter(Documento.tipo_archivo == ".pdf").scalar() or 0
    total_mb  = db.query(func.sum(Documento.tamano_mb)).scalar() or 0.0

    documentos_json  = _json_seguro([d.to_dict() for d in documentos])
    plantillas_json  = _json_seguro([p.to_dict() for p in plantillas])

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "documentos": documentos,
            "documentos_json": documentos_json,
            "plantillas_json": plantillas_json,
            "user": user,
            "user_rol": get_rol(request),
            "stats": {
                "total": total,
                "pdfs": total_pdf,
                "imagenes": total - total_pdf,
                "almacenamiento_mb": round(total_mb, 2),
            },
        },
    )


# ── Documentos: subir ─────────────────────────────────────

@app.post("/subir")
async def subir_documento(
    request: Request,
    archivo: UploadFile = File(...),
    idioma: str = Form(default="spa"),
    db: Session = Depends(get_db),
):
    if not usuario_valido(request, db):
        return RedirectResponse(url="/login", status_code=302)

    nombre_original = _nombre_seguro(archivo.filename)
    extension = os.path.splitext(nombre_original)[1].lower()
    if extension not in FORMATOS_SOPORTADOS:
        raise HTTPException(
            status_code=400,
            detail=f"Formato no soportado. Use: {', '.join(sorted(FORMATOS_SOPORTADOS))}",
        )
    _validar_contenido(archivo, extension)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    nombre_seguro = f"{timestamp}_{nombre_original}"
    ruta_destino = os.path.join(UPLOAD_DIR, nombre_seguro)

    with open(ruta_destino, "wb") as buffer:
        shutil.copyfileobj(archivo.file, buffer)

    tamano_mb = os.path.getsize(ruta_destino) / (1024 * 1024)

    try:
        texto, confianza = procesar_documento(ruta_destino, idioma)
    except Exception:
        app_log.exception("Error en OCR al procesar %s", ruta_destino)
        os.remove(ruta_destino)
        raise HTTPException(status_code=500, detail="No se pudo procesar el documento con OCR.")

    documento = Documento(
        nombre_archivo=nombre_original,
        tipo_archivo=extension,
        tamano_mb=tamano_mb,
        ruta_archivo=ruta_destino,
        texto_extraido=texto,
        idioma_ocr=idioma,
        ocr_confianza=confianza,
        subido_por=get_user(request),
    )
    db.add(documento)
    db.commit()
    db.refresh(documento)

    return RedirectResponse(url="/#explorer", status_code=303)


# ── Documentos: descargar texto OCR ──────────────────────

@app.get("/descargar/{doc_id}")
def descargar_texto(doc_id: int, request: Request, db: Session = Depends(get_db)):
    if not usuario_valido(request, db):
        raise HTTPException(status_code=401, detail="No autenticado")
    documento = db.query(Documento).filter(Documento.id == doc_id).first()
    if not documento:
        raise HTTPException(status_code=404, detail="Documento no encontrado")

    nombre_txt = os.path.splitext(documento.nombre_archivo)[0] + "_ocr.txt"
    log_seguridad("descarga_texto", request, get_user(request), f"doc_id={doc_id}")
    return PlainTextResponse(
        content=documento.texto_extraido,
        headers={"Content-Disposition": f'attachment; filename="{nombre_txt}"'},
    )


# ── Documentos: descargar archivo original ────────────────

@app.get("/archivo/{doc_id}")
def descargar_archivo_original(doc_id: int, request: Request, db: Session = Depends(get_db)):
    if not usuario_valido(request, db):
        raise HTTPException(status_code=401, detail="No autenticado")
    documento = db.query(Documento).filter(Documento.id == doc_id).first()
    if not documento:
        raise HTTPException(status_code=404, detail="Documento no encontrado")
    if not os.path.exists(documento.ruta_archivo):
        raise HTTPException(status_code=404, detail="Archivo no encontrado en disco")
    log_seguridad("descarga_original", request, get_user(request), f"doc_id={doc_id}")
    return FileResponse(
        path=documento.ruta_archivo,
        filename=documento.nombre_archivo,
        media_type="application/octet-stream",
    )


@app.get("/ver/{doc_id}")
def ver_archivo_original(doc_id: int, request: Request, db: Session = Depends(get_db)):
    if not usuario_valido(request, db):
        raise HTTPException(status_code=401, detail="No autenticado")
    documento = db.query(Documento).filter(Documento.id == doc_id).first()
    if not documento:
        raise HTTPException(status_code=404, detail="Documento no encontrado")
    if not os.path.exists(documento.ruta_archivo):
        raise HTTPException(status_code=404, detail="Archivo no encontrado en disco")
    ext = documento.tipo_archivo.lower()
    media_type_map = {
        ".pdf":  "application/pdf",
        ".png":  "image/png",
        ".jpg":  "image/jpeg",
        ".jpeg": "image/jpeg",
        ".tiff": "image/tiff",
        ".tif":  "image/tiff",
        ".bmp":  "image/bmp",
    }
    media_type = media_type_map.get(ext, "application/octet-stream")
    return FileResponse(
        path=documento.ruta_archivo,
        media_type=media_type,
        headers={"Content-Disposition": f'inline; filename="{documento.nombre_archivo}"'},
    )


# ── Documentos: eliminar ──────────────────────────────────

@app.delete("/eliminar/{doc_id}")
def eliminar_documento(doc_id: int, request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    documento = db.query(Documento).filter(Documento.id == doc_id).first()
    if not documento:
        raise HTTPException(status_code=404, detail="Documento no encontrado")

    if os.path.exists(documento.ruta_archivo):
        os.remove(documento.ruta_archivo)

    db.delete(documento)
    db.commit()
    log_seguridad("documento_eliminado", request, get_user(request),
                  f"doc_id={doc_id} nombre={documento.nombre_archivo}")
    return {"mensaje": f"Documento '{documento.nombre_archivo}' eliminado correctamente"}


# ── Plantillas OCR: CRUD ──────────────────────────────────

@app.get("/api/plantillas")
def listar_plantillas(request: Request, db: Session = Depends(get_db)):
    require_sesion(request, db)
    plantillas = db.query(PlantillaOCR).order_by(PlantillaOCR.fecha_creacion.desc()).all()
    return [p.to_dict() for p in plantillas]


@app.get("/api/plantillas/{plantilla_id}")
def obtener_plantilla(plantilla_id: int, request: Request, db: Session = Depends(get_db)):
    require_sesion(request, db)
    plantilla = db.query(PlantillaOCR).filter(PlantillaOCR.id == plantilla_id).first()
    if not plantilla:
        raise HTTPException(status_code=404, detail="Plantilla no encontrada")
    return plantilla.to_dict()


@app.post("/api/plantillas")
async def crear_plantilla(
    request: Request,
    nombre: str = Form(...),
    descripcion: str = Form(default=""),
    imagen_ref: UploadFile = File(...),
    db: Session = Depends(get_db),
):
    require_admin(request, db)

    extension = os.path.splitext(_nombre_seguro(imagen_ref.filename))[1].lower()
    if extension not in FORMATOS_IMAGEN:
        raise HTTPException(
            status_code=400,
            detail=f"La imagen de referencia debe ser: {', '.join(sorted(FORMATOS_IMAGEN))}",
        )
    _validar_contenido(imagen_ref, extension)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    nombre_archivo = f"ref_{timestamp}{extension}"
    ruta_ref = os.path.join(REFERENCIAS_DIR, nombre_archivo)

    with open(ruta_ref, "wb") as f:
        shutil.copyfileobj(imagen_ref.file, f)

    with Image.open(ruta_ref) as img:
        ancho, alto = img.size

    plantilla = PlantillaOCR(
        nombre=nombre,
        descripcion=descripcion,
        ruta_imagen_ref=ruta_ref.replace("\\", "/"),
        img_ancho=ancho,
        img_alto=alto,
    )
    db.add(plantilla)
    db.commit()
    db.refresh(plantilla)
    return plantilla.to_dict()


@app.delete("/api/plantillas/{plantilla_id}")
def eliminar_plantilla(plantilla_id: int, request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    plantilla = db.query(PlantillaOCR).filter(PlantillaOCR.id == plantilla_id).first()
    if not plantilla:
        raise HTTPException(status_code=404, detail="Plantilla no encontrada")

    if os.path.exists(plantilla.ruta_imagen_ref):
        os.remove(plantilla.ruta_imagen_ref)

    db.delete(plantilla)
    db.commit()
    log_seguridad("plantilla_eliminada", request, get_user(request),
                  f"plantilla_id={plantilla_id} nombre={plantilla.nombre}")
    return {"mensaje": f"Plantilla '{plantilla.nombre}' eliminada"}


# ── Plantillas OCR: guardar zonas ─────────────────────────

@app.put("/api/plantillas/{plantilla_id}/zonas")
def guardar_zonas(
    plantilla_id: int,
    payload: ZonasPayload,
    request: Request,
    db: Session = Depends(get_db),
):
    require_admin(request, db)
    plantilla = db.query(PlantillaOCR).filter(PlantillaOCR.id == plantilla_id).first()
    if not plantilla:
        raise HTTPException(status_code=404, detail="Plantilla no encontrada")

    # Reemplazar todas las zonas
    db.query(ZonaOCR).filter(ZonaOCR.plantilla_id == plantilla_id).delete()
    for i, z in enumerate(payload.zonas):
        zona = ZonaOCR(
            plantilla_id=plantilla_id,
            nombre_campo=z.nombre_campo,
            x=z.x, y=z.y,
            ancho=z.ancho, alto=z.alto,
            orden=i,
        )
        db.add(zona)

    db.commit()
    db.refresh(plantilla)
    return plantilla.to_dict()


# ── Plantillas OCR: procesar documento con zonas ──────────

@app.post("/api/plantillas/{plantilla_id}/procesar")
async def procesar_con_zonas(
    plantilla_id: int,
    request: Request,
    archivo: UploadFile = File(...),
    idioma: str = Form(default="spa"),
    pagina_pdf: int = Form(default=0),
    db: Session = Depends(get_db),
):
    if not usuario_valido(request, db):
        raise HTTPException(status_code=401, detail="No autenticado")

    plantilla = db.query(PlantillaOCR).filter(PlantillaOCR.id == plantilla_id).first()
    if not plantilla:
        raise HTTPException(status_code=404, detail="Plantilla no encontrada")

    if not plantilla.zonas:
        raise HTTPException(status_code=400, detail="La plantilla no tiene zonas definidas")

    nombre_original = _nombre_seguro(archivo.filename)
    extension = os.path.splitext(nombre_original)[1].lower()
    if extension not in FORMATOS_SOPORTADOS:
        raise HTTPException(
            status_code=400,
            detail=f"Formato no soportado. Use: {', '.join(sorted(FORMATOS_SOPORTADOS))}",
        )
    _validar_contenido(archivo, extension)

    # Guardar el archivo definitivamente en uploads/
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    nombre_seguro = f"{timestamp}_{nombre_original}"
    ruta_destino = os.path.join(UPLOAD_DIR, nombre_seguro)

    with open(ruta_destino, "wb") as f:
        shutil.copyfileobj(archivo.file, f)

    tamano_mb = os.path.getsize(ruta_destino) / (1024 * 1024)

    try:
        zonas_list = [z.to_dict() for z in plantilla.zonas]
        resultados = procesar_por_zonas(
            ruta_destino,
            zonas_list,
            plantilla.img_ancho,
            plantilla.img_alto,
            idioma,
            pagina_pdf,
        )
    except Exception:
        app_log.exception("Error en OCR por zonas al procesar %s", ruta_destino)
        os.remove(ruta_destino)
        raise HTTPException(status_code=500, detail="No se pudo procesar el documento con OCR por zonas.")

    texto_legible = "\n".join(
        f"{r['nombre_campo']}: {r['texto_extraido']}" for r in resultados
    )
    scores = [r["confianza"] for r in resultados if r.get("confianza", 0) > 0]
    confianza_zonas = round(sum(scores) / len(scores), 4) if scores else 0.0

    documento = Documento(
        nombre_archivo=nombre_original,
        tipo_archivo=extension,
        tamano_mb=tamano_mb,
        ruta_archivo=ruta_destino,
        texto_extraido=texto_legible,
        idioma_ocr=idioma,
        ocr_confianza=confianza_zonas,
        plantilla_ocr_id=plantilla_id,
        zona_resultados_json=json.dumps(resultados, ensure_ascii=False),
        subido_por=get_user(request),
    )
    db.add(documento)
    db.commit()
    db.refresh(documento)

    return {
        "plantilla": plantilla.nombre,
        "archivo": nombre_original,
        "doc_id": documento.id,
        "resultados": resultados,
    }


# ── Plantillas OCR: preview (extrae sin guardar) ─────────

@app.post("/api/plantillas/{plantilla_id}/preview")
async def preview_zonas(
    plantilla_id: int,
    request: Request,
    archivo: UploadFile = File(...),
    idioma: str = Form(default="spa"),
    pagina_pdf: int = Form(default=0),
    db: Session = Depends(get_db),
):
    if not usuario_valido(request, db):
        raise HTTPException(status_code=401, detail="No autenticado")

    plantilla = db.query(PlantillaOCR).filter(PlantillaOCR.id == plantilla_id).first()
    if not plantilla:
        raise HTTPException(status_code=404, detail="Plantilla no encontrada")
    if not plantilla.zonas:
        raise HTTPException(status_code=400, detail="La plantilla no tiene zonas definidas")

    nombre_original = _nombre_seguro(archivo.filename)
    extension = os.path.splitext(nombre_original)[1].lower()
    if extension not in FORMATOS_SOPORTADOS:
        raise HTTPException(status_code=400, detail=f"Formato no soportado: {extension}")
    _validar_contenido(archivo, extension)

    # Guardar en carpeta temporal con clave única
    temp_key = f"{uuid.uuid4().hex}{extension}"
    ruta_tmp = os.path.join(TMP_DIR, temp_key)

    with open(ruta_tmp, "wb") as f:
        shutil.copyfileobj(archivo.file, f)

    try:
        zonas_list = [z.to_dict() for z in plantilla.zonas]
        resultados = procesar_por_zonas(
            ruta_tmp, zonas_list,
            plantilla.img_ancho, plantilla.img_alto,
            idioma, pagina_pdf,
        )
    except Exception:
        app_log.exception("Error en OCR por zonas (preview) al procesar %s", ruta_tmp)
        if os.path.exists(ruta_tmp):
            os.remove(ruta_tmp)
        raise HTTPException(status_code=500, detail="No se pudo procesar el documento con OCR por zonas.")

    # Guardar en cache para la etapa de confirmación
    _preview_cache[temp_key] = {
        "ruta_tmp": ruta_tmp,
        "archivo_nombre": nombre_original,
        "idioma": idioma,
        "plantilla_id": plantilla_id,
        "plantilla_nombre": plantilla.nombre,
        "resultados": resultados,
    }

    return {
        "temp_key": temp_key,
        "plantilla_nombre": plantilla.nombre,
        "archivo_nombre": nombre_original,
        "resultados": resultados,
    }


# ── Plantillas OCR: confirmar (guarda tras preview) ───────

@app.post("/api/plantillas/confirmar")
async def confirmar_zona(
    request: Request,
    temp_key: str = Form(...),
    resultados_editados: Optional[str] = Form(default=None),
    db: Session = Depends(get_db),
):
    if not usuario_valido(request, db):
        raise HTTPException(status_code=401, detail="No autenticado")

    cache = _preview_cache.pop(temp_key, None)
    if not cache:
        raise HTTPException(
            status_code=404,
            detail="Previsualización no encontrada o expirada. Vuelve a subir el archivo.",
        )

    ruta_tmp = cache["ruta_tmp"]
    if not os.path.exists(ruta_tmp):
        raise HTTPException(status_code=404, detail="Archivo temporal no encontrado en disco.")

    extension = os.path.splitext(ruta_tmp)[1].lower()
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    nombre_seguro = f"{timestamp}_{cache['archivo_nombre']}"
    ruta_destino = os.path.join(UPLOAD_DIR, nombre_seguro)

    shutil.move(ruta_tmp, ruta_destino)
    tamano_mb = os.path.getsize(ruta_destino) / (1024 * 1024)

    resultados = [dict(r) for r in cache["resultados"]]

    if resultados_editados:
        try:
            edits = json.loads(resultados_editados)
            for edit in edits:
                idx = edit.get("idx")
                if isinstance(idx, int) and 0 <= idx < len(resultados):
                    resultados[idx]["texto_extraido"] = str(edit.get("texto_extraido", ""))
        except Exception:
            # Si falla el parseo se conservan los resultados originales
            app_log.warning("resultados_editados inválido para %s; se conservan los originales", temp_key)

    texto_legible = "\n".join(
        f"{r['nombre_campo']}: {r['texto_extraido']}" for r in resultados
    )
    scores = [r["confianza"] for r in resultados if r.get("confianza", 0) > 0]
    confianza_zonas = round(sum(scores) / len(scores), 4) if scores else 0.0

    documento = Documento(
        nombre_archivo=cache["archivo_nombre"],
        tipo_archivo=extension,
        tamano_mb=tamano_mb,
        ruta_archivo=ruta_destino,
        texto_extraido=texto_legible,
        idioma_ocr=cache["idioma"],
        ocr_confianza=confianza_zonas,
        plantilla_ocr_id=cache["plantilla_id"],
        zona_resultados_json=json.dumps(resultados, ensure_ascii=False),
        subido_por=get_user(request),
    )
    db.add(documento)
    db.commit()
    db.refresh(documento)

    return documento.to_dict()


# ── Plantillas OCR: cancelar preview ─────────────────────

@app.delete("/api/plantillas/temp/{temp_key}")
def cancelar_preview(temp_key: str, request: Request, db: Session = Depends(get_db)):
    if not usuario_valido(request, db):
        raise HTTPException(status_code=401, detail="No autenticado")
    if any(c in temp_key for c in ("/", "\\", "..")):
        raise HTTPException(status_code=400, detail="temp_key inválido")
    cache = _preview_cache.pop(temp_key, None)
    if cache and os.path.exists(cache["ruta_tmp"]):
        os.remove(cache["ruta_tmp"])
    return {"ok": True}


# ── Escáner WIA ──────────────────────────────────────────

@app.get("/scan/dispositivos")
def scan_dispositivos(request: Request, db: Session = Depends(get_db)):
    if not usuario_valido(request, db):
        raise HTTPException(status_code=401, detail="No autenticado")
    try:
        from core.scanner import listar_dispositivos
        return listar_dispositivos()
    except ImportError:
        raise HTTPException(status_code=503, detail="WIA no disponible en este sistema operativo")
    except Exception:
        app_log.exception("Error al listar escáneres")
        raise HTTPException(status_code=500, detail="No se pudo obtener la lista de escáneres.")


@app.post("/scan/capturar")
async def scan_capturar(
    request: Request,
    device_id: str = Form(...),
    dpi: int = Form(300),
    modo: str = Form("color"),
    db: Session = Depends(get_db),
):
    if not usuario_valido(request, db):
        raise HTTPException(status_code=401, detail="No autenticado")
    try:
        from core.scanner import escanear
        ruta = escanear(device_id, dpi, modo, directorio=TMP_DIR)
        nombre = os.path.basename(ruta)
        return {"ok": True, "temp_key": nombre, "url_preview": f"/uploads/tmp/{nombre}"}
    except ImportError:
        raise HTTPException(status_code=503, detail="WIA no disponible en este sistema operativo")
    except Exception:
        app_log.exception("Error al escanear con el dispositivo %s", device_id)
        raise HTTPException(status_code=500, detail="No se pudo completar el escaneo.")


@app.post("/scan/procesar")
async def scan_procesar(
    request: Request,
    temp_key: str = Form(...),
    idioma: str = Form("spa"),
    db: Session = Depends(get_db),
):
    if not usuario_valido(request, db):
        raise HTTPException(status_code=401, detail="No autenticado")
    if any(c in temp_key for c in ("/", "\\", "..")):
        raise HTTPException(status_code=400, detail="temp_key inválido")
    ruta_tmp = os.path.join(TMP_DIR, temp_key)
    if not os.path.exists(ruta_tmp):
        raise HTTPException(status_code=404, detail="Archivo temporal no encontrado")
    try:
        texto, confianza = procesar_documento(ruta_tmp, idioma)
    except Exception:
        app_log.exception("Error en OCR al procesar el escaneo %s", ruta_tmp)
        raise HTTPException(status_code=500, detail="No se pudo procesar el documento con OCR.")
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    nombre_final = f"{timestamp}_{temp_key}"
    ruta_final = os.path.join(UPLOAD_DIR, nombre_final)
    shutil.move(ruta_tmp, ruta_final)
    tamano_mb = os.path.getsize(ruta_final) / (1024 * 1024)
    documento = Documento(
        nombre_archivo=temp_key,
        tipo_archivo=".png",
        tamano_mb=tamano_mb,
        ruta_archivo=ruta_final,
        texto_extraido=texto,
        idioma_ocr=idioma,
        ocr_confianza=confianza,
        subido_por=get_user(request),
    )
    db.add(documento)
    db.commit()
    db.refresh(documento)
    return documento.to_dict()


@app.post("/scan/preview-zonas")
async def scan_preview_zonas(
    request: Request,
    temp_key: str = Form(...),
    plantilla_id: int = Form(...),
    idioma: str = Form("spa"),
    db: Session = Depends(get_db),
):
    if not usuario_valido(request, db):
        raise HTTPException(status_code=401, detail="No autenticado")
    if any(c in temp_key for c in ("/", "\\", "..")):
        raise HTTPException(status_code=400, detail="temp_key inválido")
    ruta_tmp = os.path.join(TMP_DIR, temp_key)
    if not os.path.exists(ruta_tmp):
        raise HTTPException(status_code=404, detail="Archivo temporal no encontrado")
    plantilla = db.query(PlantillaOCR).filter(PlantillaOCR.id == plantilla_id).first()
    if not plantilla:
        raise HTTPException(status_code=404, detail="Plantilla no encontrada")
    if not plantilla.zonas:
        raise HTTPException(status_code=400, detail="La plantilla no tiene zonas definidas")
    try:
        zonas_list = [z.to_dict() for z in plantilla.zonas]
        resultados = procesar_por_zonas(
            ruta_tmp, zonas_list,
            plantilla.img_ancho, plantilla.img_alto,
            idioma, 0,
        )
    except Exception:
        app_log.exception("Error en OCR por zonas al procesar el escaneo %s", ruta_tmp)
        raise HTTPException(status_code=500, detail="No se pudo procesar el documento con OCR por zonas.")
    _preview_cache[temp_key] = {
        "ruta_tmp": ruta_tmp,
        "archivo_nombre": temp_key,
        "idioma": idioma,
        "plantilla_id": plantilla_id,
        "plantilla_nombre": plantilla.nombre,
        "resultados": resultados,
    }
    return {
        "temp_key": temp_key,
        "plantilla_nombre": plantilla.nombre,
        "archivo_nombre": temp_key,
        "resultados": resultados,
    }


# ── Entrega autenticada de archivos subidos (Mejora H-03) ──
# Reemplaza el antiguo montaje estático de /uploads. Exige sesión y evita el
# salto de directorio resolviendo la ruta dentro de UPLOAD_DIR.

@app.get("/uploads/{ruta:path}")
def servir_subido(ruta: str, request: Request, db: Session = Depends(get_db)):
    require_sesion(request, db)
    base = os.path.realpath(UPLOAD_DIR)
    destino = os.path.realpath(os.path.join(UPLOAD_DIR, ruta))
    if not destino.startswith(base + os.sep):
        raise HTTPException(status_code=400, detail="Ruta inválida")
    if not os.path.isfile(destino):
        raise HTTPException(status_code=404, detail="Archivo no encontrado")
    return FileResponse(path=destino)


# ── Documentación de la API, solo administradores (Mejora H-11) ──

@app.get("/openapi.json", include_in_schema=False)
def openapi_admin(request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    return JSONResponse(app.openapi())


@app.get("/docs", include_in_schema=False)
def docs_admin(request: Request, db: Session = Depends(get_db)):
    require_admin(request, db)
    response = get_swagger_ui_html(
        openapi_url="/openapi.json",
        title=f"{app.title} - Documentación",
        swagger_favicon_url="/static/favicon.ico",
    )


@app.get("/redoc", include_in_schema=False)
def redoc_admin(request: Request, db: Session = Depends(get_db)):
    response.headers["Content-Security-Policy"] = CSP_DOCS
    return response
    require_admin(request, db)
    response = get_redoc_html(
        openapi_url="/openapi.json",
        title=f"{app.title} - ReDoc",
        redoc_favicon_url="/static/favicon.ico",
    )


# ── API general ───────────────────────────────────────────

    response.headers["Content-Security-Policy"] = CSP_DOCS
    return response
@app.get("/api/documentos")
def listar_documentos(request: Request, busqueda: str = "", db: Session = Depends(get_db)):
    require_sesion(request, db)
    query = db.query(Documento)
    if busqueda:
        query = query.filter(Documento.texto_extraido.contains(busqueda))
    documentos = query.order_by(Documento.fecha_carga.desc()).all()
    return [doc.to_dict() for doc in documentos]


@app.get("/api/estadisticas")
def estadisticas(request: Request, db: Session = Depends(get_db)):
    require_sesion(request, db)
    total    = db.query(func.count(Documento.id)).scalar() or 0
    by_type  = db.query(Documento.tipo_archivo, func.count(Documento.id)).group_by(Documento.tipo_archivo).all()
    total_mb = db.query(func.sum(Documento.tamano_mb)).scalar() or 0.0
    recientes = db.query(Documento).order_by(Documento.fecha_carga.desc()).limit(10).all()

    return {
        "total": total,
        "por_tipo": {t: c for t, c in by_type},
        "almacenamiento_mb": round(total_mb, 2),
        "recientes": [d.to_dict() for d in recientes],
    }
