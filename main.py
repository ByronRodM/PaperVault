import hashlib
import json
import os
import secrets
import shutil
import uuid
from datetime import datetime
from typing import List, Optional

from fastapi import FastAPI, UploadFile, File, Depends, HTTPException, Request, Form
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, FileResponse
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

app = FastAPI(
    title="PaperVault — Sistema de Gestión Documental",
    description="Sistema para escanear, procesar y gestionar documentos mediante OCR",
    version="1.0.0",
)

app.add_middleware(SessionMiddleware, secret_key="papervault-secret-key-dev-2024")
app.mount("/static", StaticFiles(directory="static"), name="static")
app.mount("/uploads", StaticFiles(directory="uploads"), name="uploads")
templates = Jinja2Templates(directory="templates")


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

def require_admin(request: Request):
    if not get_user(request):
        raise HTTPException(status_code=302, headers={"Location": "/login"})
    if get_rol(request) != "admin":
        raise HTTPException(status_code=403, detail="Se requiere rol de administrador.")


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
    usuario = db.query(Usuario).filter(
        Usuario.nombre_usuario == username,
        Usuario.activo == True,
    ).first()
    if not usuario or not _verificar(password, usuario.password_hash):
        return templates.TemplateResponse(
            "login.html",
            {"request": request, "error": "Usuario o contraseña incorrectos."},
            status_code=401,
        )
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
def cambiar_password_get(request: Request):
    if not get_user(request):
        return RedirectResponse(url="/login", status_code=302)
    return templates.TemplateResponse("cambiar_password.html", {"request": request, "error": None})


@app.post("/cambiar-password")
async def cambiar_password_post(
    request: Request,
    password_nuevo: str = Form(...),
    password_confirmar: str = Form(...),
    db: Session = Depends(get_db),
):
    user = get_user(request)
    if not user:
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
    usuario = db.query(Usuario).filter(Usuario.nombre_usuario == user).first()
    if not usuario:
        return RedirectResponse(url="/login", status_code=302)
    usuario.password_hash = _hashear(password_nuevo)
    usuario.primer_login = False
    db.commit()
    return RedirectResponse(url="/", status_code=303)


# ── API: Gestión de usuarios (solo admin) ─────────────────

@app.get("/api/usuarios")
def listar_usuarios(request: Request, db: Session = Depends(get_db)):
    require_admin(request)
    return [u.to_dict() for u in db.query(Usuario).order_by(Usuario.fecha_creacion).all()]


@app.post("/api/usuarios")
async def crear_usuario(
    request: Request,
    nombre_usuario: str = Form(...),
    rol: str = Form(default="usuario"),
    db: Session = Depends(get_db),
):
    require_admin(request)
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
    data = nuevo.to_dict()
    data["password_inicial"] = password_inicial   # solo se devuelve una vez
    return data


@app.delete("/api/usuarios/{usuario_id}")
def eliminar_usuario(request: Request, usuario_id: int, db: Session = Depends(get_db)):
    require_admin(request)
    u = db.query(Usuario).filter(Usuario.id == usuario_id).first()
    if not u:
        raise HTTPException(status_code=404, detail="Usuario no encontrado.")
    if u.nombre_usuario == "admin":
        raise HTTPException(status_code=400, detail="No se puede eliminar al administrador principal.")
    db.delete(u)
    db.commit()
    return {"ok": True}


@app.post("/api/usuarios/{usuario_id}/reset")
def resetear_password(request: Request, usuario_id: int, db: Session = Depends(get_db)):
    require_admin(request)
    u = db.query(Usuario).filter(Usuario.id == usuario_id).first()
    if not u:
        raise HTTPException(status_code=404, detail="Usuario no encontrado.")
    nueva = secrets.token_urlsafe(10)
    u.password_hash = _hashear(nueva)
    u.primer_login = True
    db.commit()
    return {"password_temporal": nueva}


# ── Página principal (Dashboard) ──────────────────────────

@app.get("/", response_class=HTMLResponse)
def pagina_principal(request: Request, db: Session = Depends(get_db)):
    user = get_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    documentos = db.query(Documento).order_by(Documento.fecha_carga.desc()).all()
    plantillas = db.query(PlantillaOCR).order_by(PlantillaOCR.fecha_creacion.desc()).all()

    total     = db.query(func.count(Documento.id)).scalar() or 0
    total_pdf = db.query(func.count(Documento.id)).filter(Documento.tipo_archivo == ".pdf").scalar() or 0
    total_mb  = db.query(func.sum(Documento.tamano_mb)).scalar() or 0.0

    documentos_json  = json.dumps([d.to_dict() for d in documentos], ensure_ascii=False)
    plantillas_json  = json.dumps([p.to_dict() for p in plantillas], ensure_ascii=False)

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
    if not get_user(request):
        return RedirectResponse(url="/login", status_code=302)

    extension = os.path.splitext(archivo.filename)[1].lower()
    if extension not in FORMATOS_SOPORTADOS:
        raise HTTPException(
            status_code=400,
            detail=f"Formato no soportado. Use: {', '.join(sorted(FORMATOS_SOPORTADOS))}",
        )

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    nombre_seguro = f"{timestamp}_{archivo.filename}"
    ruta_destino = os.path.join(UPLOAD_DIR, nombre_seguro)

    with open(ruta_destino, "wb") as buffer:
        shutil.copyfileobj(archivo.file, buffer)

    tamano_mb = os.path.getsize(ruta_destino) / (1024 * 1024)

    try:
        texto, confianza = procesar_documento(ruta_destino, idioma)
    except Exception as e:
        os.remove(ruta_destino)
        raise HTTPException(status_code=500, detail=f"Error en OCR: {str(e)}")

    documento = Documento(
        nombre_archivo=archivo.filename,
        tipo_archivo=extension,
        tamano_mb=tamano_mb,
        ruta_archivo=ruta_destino,
        texto_extraido=texto,
        idioma_ocr=idioma,
        ocr_confianza=confianza,
    )
    db.add(documento)
    db.commit()
    db.refresh(documento)

    return RedirectResponse(url="/#explorer", status_code=303)


# ── Documentos: descargar texto OCR ──────────────────────

@app.get("/descargar/{doc_id}")
def descargar_texto(doc_id: int, request: Request, db: Session = Depends(get_db)):
    if not get_user(request):
        raise HTTPException(status_code=401, detail="No autenticado")
    documento = db.query(Documento).filter(Documento.id == doc_id).first()
    if not documento:
        raise HTTPException(status_code=404, detail="Documento no encontrado")

    nombre_txt = os.path.splitext(documento.nombre_archivo)[0] + "_ocr.txt"
    return PlainTextResponse(
        content=documento.texto_extraido,
        headers={"Content-Disposition": f'attachment; filename="{nombre_txt}"'},
    )


# ── Documentos: descargar archivo original ────────────────

@app.get("/archivo/{doc_id}")
def descargar_archivo_original(doc_id: int, request: Request, db: Session = Depends(get_db)):
    if not get_user(request):
        raise HTTPException(status_code=401, detail="No autenticado")
    documento = db.query(Documento).filter(Documento.id == doc_id).first()
    if not documento:
        raise HTTPException(status_code=404, detail="Documento no encontrado")
    if not os.path.exists(documento.ruta_archivo):
        raise HTTPException(status_code=404, detail="Archivo no encontrado en disco")
    return FileResponse(
        path=documento.ruta_archivo,
        filename=documento.nombre_archivo,
        media_type="application/octet-stream",
    )


# ── Documentos: eliminar ──────────────────────────────────

@app.delete("/eliminar/{doc_id}")
def eliminar_documento(doc_id: int, request: Request, db: Session = Depends(get_db)):
    if not get_user(request):
        raise HTTPException(status_code=401, detail="No autenticado")
    documento = db.query(Documento).filter(Documento.id == doc_id).first()
    if not documento:
        raise HTTPException(status_code=404, detail="Documento no encontrado")

    if os.path.exists(documento.ruta_archivo):
        os.remove(documento.ruta_archivo)

    db.delete(documento)
    db.commit()
    return {"mensaje": f"Documento '{documento.nombre_archivo}' eliminado correctamente"}


# ── Plantillas OCR: CRUD ──────────────────────────────────

@app.get("/api/plantillas")
def listar_plantillas(db: Session = Depends(get_db)):
    plantillas = db.query(PlantillaOCR).order_by(PlantillaOCR.fecha_creacion.desc()).all()
    return [p.to_dict() for p in plantillas]


@app.get("/api/plantillas/{plantilla_id}")
def obtener_plantilla(plantilla_id: int, db: Session = Depends(get_db)):
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
    require_admin(request)

    extension = os.path.splitext(imagen_ref.filename)[1].lower()
    if extension not in FORMATOS_IMAGEN:
        raise HTTPException(
            status_code=400,
            detail=f"La imagen de referencia debe ser: {', '.join(sorted(FORMATOS_IMAGEN))}",
        )

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
    require_admin(request)
    plantilla = db.query(PlantillaOCR).filter(PlantillaOCR.id == plantilla_id).first()
    if not plantilla:
        raise HTTPException(status_code=404, detail="Plantilla no encontrada")

    if os.path.exists(plantilla.ruta_imagen_ref):
        os.remove(plantilla.ruta_imagen_ref)

    db.delete(plantilla)
    db.commit()
    return {"mensaje": f"Plantilla '{plantilla.nombre}' eliminada"}


# ── Plantillas OCR: guardar zonas ─────────────────────────

@app.put("/api/plantillas/{plantilla_id}/zonas")
def guardar_zonas(
    plantilla_id: int,
    payload: ZonasPayload,
    request: Request,
    db: Session = Depends(get_db),
):
    require_admin(request)
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
    if not get_user(request):
        raise HTTPException(status_code=401, detail="No autenticado")

    plantilla = db.query(PlantillaOCR).filter(PlantillaOCR.id == plantilla_id).first()
    if not plantilla:
        raise HTTPException(status_code=404, detail="Plantilla no encontrada")

    if not plantilla.zonas:
        raise HTTPException(status_code=400, detail="La plantilla no tiene zonas definidas")

    extension = os.path.splitext(archivo.filename)[1].lower()
    if extension not in FORMATOS_SOPORTADOS:
        raise HTTPException(
            status_code=400,
            detail=f"Formato no soportado. Use: {', '.join(sorted(FORMATOS_SOPORTADOS))}",
        )

    # Guardar el archivo definitivamente en uploads/
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    nombre_seguro = f"{timestamp}_{archivo.filename}"
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
    except Exception as e:
        os.remove(ruta_destino)
        raise HTTPException(status_code=500, detail=f"Error en OCR por zonas: {str(e)}")

    texto_legible = "\n".join(
        f"{r['nombre_campo']}: {r['texto_extraido']}" for r in resultados
    )
    scores = [r["confianza"] for r in resultados if r.get("confianza", 0) > 0]
    confianza_zonas = round(sum(scores) / len(scores), 4) if scores else 0.0

    documento = Documento(
        nombre_archivo=archivo.filename,
        tipo_archivo=extension,
        tamano_mb=tamano_mb,
        ruta_archivo=ruta_destino,
        texto_extraido=texto_legible,
        idioma_ocr=idioma,
        ocr_confianza=confianza_zonas,
        plantilla_ocr_id=plantilla_id,
        zona_resultados_json=json.dumps(resultados, ensure_ascii=False),
    )
    db.add(documento)
    db.commit()
    db.refresh(documento)

    return {
        "plantilla": plantilla.nombre,
        "archivo": archivo.filename,
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
    if not get_user(request):
        raise HTTPException(status_code=401, detail="No autenticado")

    plantilla = db.query(PlantillaOCR).filter(PlantillaOCR.id == plantilla_id).first()
    if not plantilla:
        raise HTTPException(status_code=404, detail="Plantilla no encontrada")
    if not plantilla.zonas:
        raise HTTPException(status_code=400, detail="La plantilla no tiene zonas definidas")

    extension = os.path.splitext(archivo.filename)[1].lower()
    if extension not in FORMATOS_SOPORTADOS:
        raise HTTPException(status_code=400, detail=f"Formato no soportado: {extension}")

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
    except Exception as e:
        if os.path.exists(ruta_tmp):
            os.remove(ruta_tmp)
        raise HTTPException(status_code=500, detail=f"Error en OCR: {str(e)}")

    # Guardar en cache para la etapa de confirmación
    _preview_cache[temp_key] = {
        "ruta_tmp": ruta_tmp,
        "archivo_nombre": archivo.filename,
        "idioma": idioma,
        "plantilla_id": plantilla_id,
        "plantilla_nombre": plantilla.nombre,
        "resultados": resultados,
    }

    return {
        "temp_key": temp_key,
        "plantilla_nombre": plantilla.nombre,
        "archivo_nombre": archivo.filename,
        "resultados": resultados,
    }


# ── Plantillas OCR: confirmar (guarda tras preview) ───────

@app.post("/api/plantillas/confirmar")
async def confirmar_zona(
    request: Request,
    temp_key: str = Form(...),
    db: Session = Depends(get_db),
):
    if not get_user(request):
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

    resultados = cache["resultados"]
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
    )
    db.add(documento)
    db.commit()
    db.refresh(documento)

    return documento.to_dict()


# ── Plantillas OCR: cancelar preview ─────────────────────

@app.delete("/api/plantillas/temp/{temp_key}")
def cancelar_preview(temp_key: str, request: Request):
    if not get_user(request):
        raise HTTPException(status_code=401, detail="No autenticado")
    if any(c in temp_key for c in ("/", "\\", "..")):
        raise HTTPException(status_code=400, detail="temp_key inválido")
    cache = _preview_cache.pop(temp_key, None)
    if cache and os.path.exists(cache["ruta_tmp"]):
        os.remove(cache["ruta_tmp"])
    return {"ok": True}


# ── API general ───────────────────────────────────────────

@app.get("/api/documentos")
def listar_documentos(busqueda: str = "", db: Session = Depends(get_db)):
    query = db.query(Documento)
    if busqueda:
        query = query.filter(Documento.texto_extraido.contains(busqueda))
    documentos = query.order_by(Documento.fecha_carga.desc()).all()
    return [doc.to_dict() for doc in documentos]


@app.get("/api/estadisticas")
def estadisticas(db: Session = Depends(get_db)):
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
