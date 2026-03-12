import os
import shutil
from datetime import datetime

from fastapi import FastAPI, UploadFile, File, Depends, HTTPException, Request, Form
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from database import init_db, get_db, Documento
from ocr_engine import procesar_documento, verificar_tesseract, FORMATOS_SOPORTADOS

UPLOAD_DIR = "uploads"
os.makedirs(UPLOAD_DIR, exist_ok=True)

app = FastAPI(
    title="Sistema de Gestión Documental con OCR",
    description="Sistema para escanear, procesar y gestionar documentos mediante OCR",
    version="1.0.0",
)

app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")


@app.on_event("startup")
def startup():
    init_db()
    print("Base de datos inicializada.")
    if verificar_tesseract():
        print("Motor OCR listo.")
    else:
        print("ADVERTENCIA: Tesseract no está configurado correctamente.")


# ---------- PÁGINAS WEB ----------


@app.get("/", response_class=HTMLResponse)
def pagina_principal(request: Request, busqueda: str = "", db: Session = Depends(get_db)):
    if busqueda:
        documentos = (
            db.query(Documento)
            .filter(Documento.texto_extraido.contains(busqueda))
            .order_by(Documento.fecha_carga.desc())
            .all()
        )
    else:
        documentos = db.query(Documento).order_by(Documento.fecha_carga.desc()).all()

    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "documentos": documentos,
            "busqueda": busqueda,
            "total": len(documentos),
        },
    )


# ---------- API ENDPOINTS ----------


@app.post("/subir")
async def subir_documento(
    archivo: UploadFile = File(...),
    idioma: str = Form(default="spa"),
    db: Session = Depends(get_db),
):
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

    return RedirectResponse(url="/", status_code=303)


@app.get("/documento/{doc_id}")
def ver_documento(doc_id: int, request: Request, db: Session = Depends(get_db)):
    documento = db.query(Documento).filter(Documento.id == doc_id).first()
    if not documento:
        raise HTTPException(status_code=404, detail="Documento no encontrado")
    return templates.TemplateResponse(
        "index.html",
        {
            "request": request,
            "documentos": [documento],
            "documento_detalle": documento,
            "busqueda": "",
            "total": 1,
        },
    )


@app.get("/descargar/{doc_id}")
def descargar_texto(doc_id: int, db: Session = Depends(get_db)):
    documento = db.query(Documento).filter(Documento.id == doc_id).first()
    if not documento:
        raise HTTPException(status_code=404, detail="Documento no encontrado")

    nombre_txt = os.path.splitext(documento.nombre_archivo)[0] + "_ocr.txt"
    return PlainTextResponse(
        content=documento.texto_extraido,
        headers={"Content-Disposition": f'attachment; filename="{nombre_txt}"'},
    )


@app.delete("/eliminar/{doc_id}")
def eliminar_documento(doc_id: int, db: Session = Depends(get_db)):
    documento = db.query(Documento).filter(Documento.id == doc_id).first()
    if not documento:
        raise HTTPException(status_code=404, detail="Documento no encontrado")

    if os.path.exists(documento.ruta_archivo):
        os.remove(documento.ruta_archivo)

    db.delete(documento)
    db.commit()
    return {"mensaje": f"Documento '{documento.nombre_archivo}' eliminado correctamente"}


@app.get("/api/documentos")
def listar_documentos(busqueda: str = "", db: Session = Depends(get_db)):
    query = db.query(Documento)
    if busqueda:
        query = query.filter(Documento.texto_extraido.contains(busqueda))
    documentos = query.order_by(Documento.fecha_carga.desc()).all()
    return [doc.to_dict() for doc in documentos]
