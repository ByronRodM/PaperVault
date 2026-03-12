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
