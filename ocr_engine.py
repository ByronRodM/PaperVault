import pytesseract
from PIL import Image
import cv2
import numpy as np
import os
from pdf2image import convert_from_path

# Ruta de Tesseract en Windows (ajustar si se instaló en otra ubicación)
pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

FORMATOS_IMAGEN = {".png", ".jpg", ".jpeg", ".tiff", ".tif", ".bmp"}
FORMATOS_PDF = {".pdf"}
FORMATOS_SOPORTADOS = FORMATOS_IMAGEN | FORMATOS_PDF


def preprocesar_imagen(imagen: np.ndarray) -> np.ndarray:
    """Mejora la imagen para obtener mejores resultados del OCR."""
    gris = cv2.cvtColor(imagen, cv2.COLOR_BGR2GRAY)
    gris = cv2.medianBlur(gris, 3)
    binaria = cv2.adaptiveThreshold(
        gris, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, cv2.THRESH_BINARY, 11, 2
    )
    return binaria


def extraer_texto_imagen(ruta_imagen: str, idioma: str = "spa") -> str:
    """Extrae texto de un archivo de imagen usando Tesseract OCR."""
    imagen_cv = cv2.imread(ruta_imagen)
    if imagen_cv is None:
        raise ValueError(f"No se pudo leer la imagen: {ruta_imagen}")

    imagen_procesada = preprocesar_imagen(imagen_cv)
    pil_imagen = Image.fromarray(imagen_procesada)
    texto = pytesseract.image_to_string(pil_imagen, lang=idioma)
    return texto.strip()


def extraer_texto_pdf(ruta_pdf: str, idioma: str = "spa") -> str:
    """Extrae texto de un archivo PDF escaneado, página por página."""
    paginas = convert_from_path(ruta_pdf, dpi=300)
    texto_completo = []

    for i, pagina in enumerate(paginas):
        imagen_cv = cv2.cvtColor(np.array(pagina), cv2.COLOR_RGB2BGR)
        imagen_procesada = preprocesar_imagen(imagen_cv)
        pil_imagen = Image.fromarray(imagen_procesada)
        texto = pytesseract.image_to_string(pil_imagen, lang=idioma)
        texto_completo.append(f"--- Página {i + 1} ---\n{texto.strip()}")

    return "\n\n".join(texto_completo)


def procesar_documento(ruta_archivo: str, idioma: str = "spa") -> str:
    """Punto de entrada principal: detecta el tipo de archivo y extrae el texto."""
    extension = os.path.splitext(ruta_archivo)[1].lower()

    if extension not in FORMATOS_SOPORTADOS:
        raise ValueError(
            f"Formato no soportado: {extension}. "
            f"Formatos válidos: {', '.join(sorted(FORMATOS_SOPORTADOS))}"
        )

    if extension in FORMATOS_PDF:
        return extraer_texto_pdf(ruta_archivo, idioma)
    else:
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

    resultados = []
    for zona in zonas:
        x = int(zona["x"] * escala_x)
        y = int(zona["y"] * escala_y)
        w = int(zona["ancho"] * escala_x)
        h = int(zona["alto"]  * escala_y)

        # Garantizar que el recorte quede dentro de la imagen
        x = max(0, min(x, w_real - 1))
        y = max(0, min(y, h_real - 1))
        w = max(1, min(w, w_real - x))
        h = max(1, min(h, h_real - y))

        recorte_pil = pil_img.crop((x, y, x + w, y + h))

        # Preprocesamiento con OpenCV para mejorar precisión
        recorte_cv = cv2.cvtColor(np.array(recorte_pil), cv2.COLOR_RGB2BGR)
        recorte_proc = preprocesar_imagen(recorte_cv)
        recorte_final = Image.fromarray(recorte_proc)

        texto = pytesseract.image_to_string(recorte_final, lang=idioma).strip()
        resultados.append({
            "nombre_campo": zona["nombre_campo"],
            "texto_extraido": texto,
            "x": x, "y": y, "ancho": w, "alto": h,
        })

    return resultados


def verificar_tesseract() -> bool:
    """Verifica que Tesseract esté instalado y accesible."""
    try:
        version = pytesseract.get_tesseract_version()
        print(f"Tesseract OCR versión: {version}")
        return True
    except Exception as e:
        print(f"Error: Tesseract no encontrado. {e}")
        print("Descargue Tesseract de: https://github.com/UB-Mannheim/tesseract/wiki")
        return False
