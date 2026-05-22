from paddleocr import PaddleOCR
from PIL import Image
import cv2
import numpy as np
import os
import tempfile
from pdf2image import convert_from_path

FORMATOS_IMAGEN = {".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp"}
FORMATOS_PDF = {".pdf"}
FORMATOS_SOPORTADOS = FORMATOS_IMAGEN | FORMATOS_PDF

# Mapeo de códigos de idioma → PaddleOCR 3.x
# 'latin' fue eliminado en v3; los idiomas de escritura latina usan 'en'
LANG_MAP = {
    "spa": "en",
    "eng": "en",
    "fra": "en",
    "deu": "en",
    "ita": "en",
    "por": "en",
    "chi_sim": "ch",
    "chi_tra": "chinese_cht",
    "jpn": "japan",
    "kor": "korean",
    "ara": "arabic",
    "rus": "cyrillic",
}

# Instancias reutilizables por idioma (inicializar PaddleOCR es costoso)
_ocr_cache: dict = {}


def _get_ocr(idioma: str = "spa") -> PaddleOCR:
    """Instancia para documentos completos (pipeline completo con preprocesamiento)."""
    paddle_lang = LANG_MAP.get(idioma, "en")
    if paddle_lang not in _ocr_cache:
        _ocr_cache[paddle_lang] = PaddleOCR(lang=paddle_lang)
    return _ocr_cache[paddle_lang]


def _get_ocr_zona(idioma: str = "spa") -> PaddleOCR:
    """
    Instancia para recortes de zonas: sin corrección de orientación de documento
    ni unwarping, que distorsionan imágenes pequeñas ya rectas.
    """
    paddle_lang = LANG_MAP.get(idioma, "en")
    key = f"zona_{paddle_lang}"
    if key not in _ocr_cache:
        _ocr_cache[key] = PaddleOCR(
            lang=paddle_lang,
            use_doc_orientation_classify=False,
            use_doc_unwarping=False,
            use_textline_orientation=False,
        )
    return _ocr_cache[key]


def _item_a_dict(item) -> dict:
    """Convierte un resultado de PaddleOCR a dict independientemente del tipo."""
    if isinstance(item, dict):
        return item
    # Objeto Result de PaddleOCR 3.x: soporta acceso por clave como un dict
    if hasattr(item, "to_dict"):
        return item.to_dict()
    if hasattr(item, "__getitem__") and hasattr(item, "keys"):
        return dict(item)
    # Fallback: atributos del objeto
    if hasattr(item, "__dict__"):
        return vars(item)
    return {}


def _extraer_texto_y_confianza(resultado) -> tuple:
    """
    Extrae texto y confianza promedio de la respuesta de PaddleOCR.
    Compatible con v2.x (lista de páginas) y v3.x (generador/lista de Result).
    Retorna (texto: str, confianza: float  0-1).
    """
    lineas = []
    scores = []

    for item in resultado:
        d = _item_a_dict(item)

        if d:
            # PaddleOCR 3.x (PaddleX): claves 'rec_texts' y 'rec_scores'
            textos = d.get("rec_texts", d.get("rec_text", []))
            puntos = d.get("rec_scores", d.get("rec_score", []))
            if textos:
                lineas.extend(str(t) for t in textos if t)
                scores.extend(float(s) for s in puntos if s is not None)
                continue

        # PaddleOCR 2.x: item es lista [[box, (texto, confianza)], ...]
        if isinstance(item, (list, tuple)):
            for linea in item:
                if linea and len(linea) >= 2 and isinstance(linea[1], (list, tuple)):
                    lineas.append(str(linea[1][0]))
                    if len(linea[1]) >= 2:
                        scores.append(float(linea[1][1]))

    texto = "\n".join(lineas)
    confianza = round(sum(scores) / len(scores), 4) if scores else 0.0
    return texto, confianza


def extraer_texto_imagen(ruta_imagen: str, idioma: str = "spa") -> tuple:
    """Extrae texto de una imagen. Retorna (texto, confianza_promedio)."""
    if not os.path.exists(ruta_imagen):
        raise ValueError(f"No se pudo leer la imagen: {ruta_imagen}")
    ocr = _get_ocr(idioma)
    resultado = list(ocr.predict(ruta_imagen))
    texto, confianza = _extraer_texto_y_confianza(resultado)
    return texto.strip(), confianza


def extraer_texto_pdf(ruta_pdf: str, idioma: str = "spa") -> tuple:
    """Extrae texto de un PDF escaneado página por página. Retorna (texto, confianza_promedio)."""
    paginas = convert_from_path(ruta_pdf, dpi=300)
    ocr = _get_ocr(idioma)
    texto_completo = []
    scores_total = []
    for i, pagina in enumerate(paginas):
        imagen_cv = cv2.cvtColor(np.array(pagina), cv2.COLOR_RGB2BGR)
        resultado = list(ocr.predict(imagen_cv))
        texto, confianza = _extraer_texto_y_confianza(resultado)
        texto_completo.append(f"--- Página {i + 1} ---\n{texto.strip()}")
        if confianza > 0:
            scores_total.append(confianza)
    confianza_final = round(sum(scores_total) / len(scores_total), 4) if scores_total else 0.0
    return "\n\n".join(texto_completo), confianza_final


def procesar_documento(ruta_archivo: str, idioma: str = "spa") -> tuple:
    """Punto de entrada principal. Retorna (texto, confianza_promedio)."""
    extension = os.path.splitext(ruta_archivo)[1].lower()
    if extension not in FORMATOS_SOPORTADOS:
        raise ValueError(
            f"Formato no soportado: {extension}. "
            f"Formatos válidos: {', '.join(sorted(FORMATOS_SOPORTADOS))}"
        )
    if extension in FORMATOS_PDF:
        return extraer_texto_pdf(ruta_archivo, idioma)
    return extraer_texto_imagen(ruta_archivo, idioma)


def procesar_por_zonas(
    ruta_archivo: str,
    zonas: list,
    img_ancho_ref: int,
    img_alto_ref: int,
    idioma: str = "spa",
    pagina_pdf: int = 0,
) -> list:
    """
    Aplica OCR únicamente sobre las zonas definidas en una plantilla.

    Acepta imágenes (PNG/JPG/TIFF/BMP) y PDFs. Para PDFs, procesa la
    página indicada por `pagina_pdf` (0-based; por defecto la primera).

    Las coordenadas de `zonas` están en píxeles del archivo de referencia
    (img_ancho_ref × img_alto_ref). Si el documento tiene distinto tamaño
    las coordenadas se escalan automáticamente.

    Returns:
        Lista de dicts {nombre_campo, texto_extraido, x, y, ancho, alto}.
    """
    extension = os.path.splitext(ruta_archivo)[1].lower()

    if extension in FORMATOS_PDF:
        paginas = convert_from_path(
            ruta_archivo,
            dpi=300,
            first_page=pagina_pdf + 1,
            last_page=pagina_pdf + 1,
        )
        if not paginas:
            raise ValueError(f"No se pudo convertir la página {pagina_pdf + 1} del PDF")
        pil_img = paginas[0].convert("RGB")
    else:
        pil_img = Image.open(ruta_archivo).convert("RGB")

    w_real, h_real = pil_img.size
    escala_x = w_real / img_ancho_ref if img_ancho_ref else 1.0
    escala_y = h_real / img_alto_ref  if img_alto_ref  else 1.0

    ocr = _get_ocr_zona(idioma)
    resultados = []

    for zona in zonas:
        x = int(zona["x"] * escala_x)
        y = int(zona["y"] * escala_y)
        w = int(zona["ancho"] * escala_x)
        h = int(zona["alto"]  * escala_y)

        x = max(0, min(x, w_real - 1))
        y = max(0, min(y, h_real - 1))
        w = max(1, min(w, w_real - x))
        h = max(1, min(h, h_real - y))

        # Padding mínimo (2 px) solo para no cortar píxeles de borde de letra
        pad = 2
        x1 = max(0, x - pad)
        y1 = max(0, y - pad)
        x2 = min(w_real, x + w + pad)
        y2 = min(h_real, y + h + pad)

        recorte_pil = pil_img.crop((x1, y1, x2, y2))

        # Escalar si el recorte es pequeño: garantizar al menos 200 px de ancho
        # y 60 px de alto para que el detector encuentre todas las líneas
        rw, rh = recorte_pil.size
        escala = max(200 / rw if rw < 200 else 1.0,
                     60  / rh if rh < 60  else 1.0)
        if escala > 1.0:
            recorte_pil = recorte_pil.resize(
                (int(rw * escala), int(rh * escala)),
                Image.LANCZOS,
            )

        # Guardar en archivo temporal y pasar la ruta (más fiable que array)
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
            tmp_path = tmp.name
        try:
            recorte_pil.save(tmp_path, format="PNG")
            resultado = list(ocr.predict(tmp_path))
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

        texto, confianza = _extraer_texto_y_confianza(resultado)

        resultados.append({
            "nombre_campo":   zona["nombre_campo"],
            "texto_extraido": texto.strip(),
            "confianza":      confianza,
            "x": x, "y": y, "ancho": w, "alto": h,
        })

    return resultados


def verificar_paddle() -> bool:
    """Verifica que PaddleOCR esté instalado y disponible."""
    try:
        from paddleocr import PaddleOCR as _check  # noqa: F401
        print("PaddleOCR disponible correctamente.")
        return True
    except ImportError as e:
        print(f"Error: PaddleOCR no encontrado. {e}")
        return False
