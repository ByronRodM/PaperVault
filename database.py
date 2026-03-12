from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, Float
from sqlalchemy.orm import declarative_base, sessionmaker
from datetime import datetime

DATABASE_URL = "sqlite:///./gestion_documental.db"

engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False})
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

    def to_dict(self):
        return {
            "id": self.id,
            "nombre_archivo": self.nombre_archivo,
            "tipo_archivo": self.tipo_archivo,
            "tamano_mb": round(self.tamano_mb, 2),
            "texto_extraido": self.texto_extraido,
            "fecha_carga": self.fecha_carga.strftime("%d/%m/%Y %H:%M:%S"),
            "idioma_ocr": self.idioma_ocr,
        }


def init_db():
    Base.metadata.create_all(bind=engine)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
