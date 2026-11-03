import os
import json as _json

from dotenv import load_dotenv
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, Float, ForeignKey, Boolean, text
from sqlalchemy.orm import declarative_base, sessionmaker, relationship
from datetime import datetime

load_dotenv()

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql://papervault:papervault@127.0.0.1:5432/papervault",
)

# connect_timeout: sin él, en Windows psycopg puede esperar indefinidamente cuando
# "localhost" resuelve primero a ::1 y PostgreSQL solo escucha en 127.0.0.1 (H-10).
# Con el límite, el intento por IPv6 caduca y se pasa a la siguiente dirección.
engine = create_engine(DATABASE_URL, pool_pre_ping=True, connect_args={"connect_timeout": 10})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()


class Documento(Base):
    __tablename__ = "documentos"

    id = Column(Integer, primary_key=True, index=True, autoincrement=True)
    nombre_archivo = Column(String(255), nullable=False)
    tipo_archivo = Column(String(50), nullable=False)
    tamano_mb = Column(Float, nullable=False)
    ruta_archivo = Column(String(500), nullable=False)
    texto_extraido = Column(Text, default="")
    fecha_carga = Column(DateTime, default=datetime.now)
    idioma_ocr = Column(String(10), default="spa")
    ocr_confianza = Column(Float, nullable=True)
    # OCR por zonas — nulos si se procesó con OCR completo
    plantilla_ocr_id = Column(Integer, ForeignKey("plantillas_ocr.id"), nullable=True)
    zona_resultados_json = Column(Text, nullable=True)
    subido_por = Column(String(50), nullable=True)

    plantilla_ocr = relationship("PlantillaOCR", foreign_keys=[plantilla_ocr_id])

    def to_dict(self):
        zonas = None
        if self.zona_resultados_json:
            try:
                zonas = _json.loads(self.zona_resultados_json)
            except Exception:
                zonas = None
        return {
            "id": self.id,
            "nombre_archivo": self.nombre_archivo,
            "tipo_archivo": self.tipo_archivo,
            "tamano_mb": round(self.tamano_mb, 2),
            "texto_extraido": self.texto_extraido,
            "fecha_carga": self.fecha_carga.strftime("%d/%m/%Y %H:%M:%S"),
            "idioma_ocr": self.idioma_ocr,
            "ocr_confianza": round(self.ocr_confianza, 4) if self.ocr_confianza is not None else None,
            "plantilla_ocr_id": self.plantilla_ocr_id,
            "plantilla_nombre": self.plantilla_ocr.nombre if self.plantilla_ocr else None,
            "zona_resultados": zonas,
            "subido_por": self.subido_por,
        }


class PlantillaOCR(Base):
    """
    Template de OCR por zonas. Almacena una imagen de referencia y sus
    dimensiones originales para escalar correctamente las zonas al procesar
    documentos del mismo tipo.
    """
    __tablename__ = "plantillas_ocr"

    id = Column(Integer, primary_key=True, autoincrement=True)
    nombre = Column(String(255), nullable=False)
    descripcion = Column(Text, default="")
    ruta_imagen_ref = Column(String(500), nullable=False)
    img_ancho = Column(Integer, nullable=False)
    img_alto = Column(Integer, nullable=False)
    fecha_creacion = Column(DateTime, default=datetime.now)

    zonas = relationship(
        "ZonaOCR",
        back_populates="plantilla",
        cascade="all, delete-orphan",
        order_by="ZonaOCR.orden",
    )

    def to_dict(self):
        return {
            "id": self.id,
            "nombre": self.nombre,
            "descripcion": self.descripcion,
            "ruta_imagen_ref": self.ruta_imagen_ref,
            "img_ancho": self.img_ancho,
            "img_alto": self.img_alto,
            "fecha_creacion": self.fecha_creacion.strftime("%d/%m/%Y %H:%M"),
            "zonas": [z.to_dict() for z in self.zonas],
        }


class ZonaOCR(Base):
    """
    Zona de extracción de texto dentro de una plantilla.
    Coordenadas en píxeles de la imagen original de referencia.
    """
    __tablename__ = "zonas_ocr"

    id = Column(Integer, primary_key=True, autoincrement=True)
    plantilla_id = Column(Integer, ForeignKey("plantillas_ocr.id"), nullable=False)
    nombre_campo = Column(String(255), nullable=False)
    x = Column(Integer, nullable=False)
    y = Column(Integer, nullable=False)
    ancho = Column(Integer, nullable=False)
    alto = Column(Integer, nullable=False)
    orden = Column(Integer, default=0)

    plantilla = relationship("PlantillaOCR", back_populates="zonas")

    def to_dict(self):
        return {
            "id": self.id,
            "plantilla_id": self.plantilla_id,
            "nombre_campo": self.nombre_campo,
            "x": self.x,
            "y": self.y,
            "ancho": self.ancho,
            "alto": self.alto,
            "orden": self.orden,
        }


class Usuario(Base):
    """Usuarios del sistema con roles y control de primer inicio de sesión."""
    __tablename__ = "usuarios"

    id = Column(Integer, primary_key=True, autoincrement=True)
    nombre_usuario = Column(String(50), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    rol = Column(String(20), default="usuario", nullable=False)  # "admin" | "usuario"
    activo = Column(Boolean, default=True, nullable=False)
    primer_login = Column(Boolean, default=True, nullable=False)
    fecha_creacion = Column(DateTime, default=datetime.now)

    def to_dict(self):
        return {
            "id": self.id,
            "nombre_usuario": self.nombre_usuario,
            "rol": self.rol,
            "activo": self.activo,
            "primer_login": self.primer_login,
            "fecha_creacion": self.fecha_creacion.strftime("%d/%m/%Y %H:%M"),
        }


def _migrate():
    """
    Aplica migraciones incrementales sobre tablas ya existentes.
    PostgreSQL soporta ADD COLUMN IF NOT EXISTS (desde v9.6).
    """
    migraciones = [
        "ALTER TABLE documentos ADD COLUMN IF NOT EXISTS plantilla_ocr_id INTEGER REFERENCES plantillas_ocr(id)",
        "ALTER TABLE documentos ADD COLUMN IF NOT EXISTS zona_resultados_json TEXT",
        "ALTER TABLE documentos ADD COLUMN IF NOT EXISTS ocr_confianza DOUBLE PRECISION",
        "ALTER TABLE documentos ADD COLUMN IF NOT EXISTS subido_por VARCHAR(50)",
    ]
    with engine.connect() as conn:
        for sql in migraciones:
            conn.execute(text(sql))
        conn.commit()


def init_db():
    Base.metadata.create_all(bind=engine)
    _migrate()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
