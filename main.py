import json
import os
import shutil
from datetime import datetime

from fastapi import FastAPI, UploadFile, File, Depends, HTTPException, Request, Form
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from typing import Optional

from database import init_db, get_db, Documento
from ocr_engine import procesar_documento, verificar_tesseract, FORMATOS_SOPORTADOS

UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)
os.makedirs("static", exist_ok=True)

# Usuarios para el prototipo — en producción usar base de datos con hashes
USERS = {
    "admin": "admin123",
}

app = FastAPI(
    title="PaperVault — Sistema de Gestión Documental",
    description="Sistema para escanear, procesar y gestionar documentos mediante OCR",
    version="1.0.0",
)

app.add_middleware(SessionMiddleware, secret_key="papervault-secret-key-dev-2024")
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


# ── Helpers ──────────────────────────────────────────────

def get_user(request: Request) -> Optional[str]:
    return request.session.get("user")


def require_user(request: Request) -> str:
    user = get_user(request)
    if not user:
        raise HTTPException(status_code=401, detail="No autenticado")
    return user


@app.on_event("startup")
def startup():
    init_db()
    print("Base de datos inicializada.")
    if verificar_tesseract():
        print("Motor OCR listo.")
    else:
        print("ADVERTENCIA: Tesseract no está configurado correctamente.")


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
):
    if USERS.get(username) == password:
        request.session["user"] = username
        return RedirectResponse(url="/", status_code=303)
    return templates.TemplateResponse(
        "login.html",
        {"request": request, "error": "Usuario o contraseña incorrectos. Intenta de nuevo."},
        status_code=401,
    )


@app.get("/logout")
def hacer_logout(request: Request):
    request.session.clear()
    return RedirectResponse(url="/login", status_code=302)


# ── Página principal (Dashboard) ──────────────────────────

@app.get("/", response_class=HTMLResponse)
def pagina_principal(request: Request, db: Session = Depends(get_db)):
    user = get_user(request)
    if not user:
        return RedirectResponse(url="/login", status_code=302)

    documentos = db.query(Documento).order_by(Documento.fecha_carga.desc()).all()

    total     = db.query(func.count(Documento.id)).scalar() or 0
    total_pdf = db.query(func.count(Documento.id)).filter(Documento.tipo_archivo == ".pdf").scalar() or 0
    total_mb  = db.query(func.sum(Documento.tamano_mb)).scalar() or 0.0

    documentos_json = json.dumps([d.to_dict() for d in documentos], ensure_ascii=False)

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "documentos": documentos,
            "documentos_json": documentos_json,
            "user": user,
            "stats": {
                "total": total,
                "pdfs": total_pdf,
                "imagenes": total - total_pdf,
                "almacenamiento_mb": round(total_mb, 2),
            },
        },
    )


# ── Subir documento ───────────────────────────────────────

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
        texto = procesar_documento(ruta_destino, idioma)
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
    )
    db.add(documento)
    db.commit()
    db.refresh(documento)

    return RedirectResponse(url="/#explorer", status_code=303)


# ── Ver texto OCR (ruta legacy — se mantiene por compatibilidad) ──

@app.get("/documento/{doc_id}")
def ver_documento(doc_id: int, request: Request, db: Session = Depends(get_db)):
    if not get_user(request):
        return RedirectResponse(url="/login", status_code=302)
    documento = db.query(Documento).filter(Documento.id == doc_id).first()
    if not documento:
        raise HTTPException(status_code=404, detail="Documento no encontrado")
    return RedirectResponse(url="/", status_code=302)


# ── Descargar texto OCR ───────────────────────────────────

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


# ── Descargar archivo original ────────────────────────────

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


# ── Eliminar documento ────────────────────────────────────

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


# ── API ───────────────────────────────────────────────────

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
