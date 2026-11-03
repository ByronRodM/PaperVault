# PaperVault

Sistema de Gestión Documental con OCR para la digitalización, indexación y consulta de documentos escaneados.

## Características principales

- **OCR completo** — extrae texto de imágenes y PDFs escaneados mediante PaddleOCR
- **OCR por zonas** — define plantillas con regiones de interés para documentos estructurados (facturas, formularios)
- **Flujo preview/confirmar** — previsualiza los campos extraídos antes de guardar; los textos son editables para correcciones manuales previas al almacenamiento
- **Visor de documentos** — visualiza el archivo original directamente en la interfaz, con zoom mediante rueda del ratón o botones y paneo con arrastre (imágenes); visor nativo del navegador para PDFs
- **Explorador de documentos** — búsqueda por texto extraído, filtros por tipo, ordenación y vista de cuadrícula/lista
- **Reportes** — tabla de campos por plantilla OCR, suma de totales por mes y gráfico de barras; actividad reciente y distribución por tipo de archivo
- **Gestión de usuarios** — roles `admin` / `usuario`, contraseña temporal en primer ingreso, reset por administrador
- **Confianza OCR** — indicador visual del porcentaje de confianza por documento

## Stack tecnológico

| Capa | Tecnología |
|------|-----------|
| Backend | FastAPI + Uvicorn |
| Plantillas | Jinja2 |
| Base de datos | PostgreSQL 16 (Docker) |
| ORM | SQLAlchemy |
| OCR | PaddleOCR 3.x (PaddlePaddle) |
| Procesamiento | Pillow, OpenCV, pdf2image |
| Autenticación | Starlette SessionMiddleware + PBKDF2-SHA256 |
| Frontend | HTML/CSS/JS vanilla + Chart.js + Bootstrap Icons |

## Prerrequisitos

- Python 3.8+
- Docker Desktop
- Poppler (requerido por `pdf2image` para convertir PDFs)

## Configuración

### 1. Variables de entorno

Copia `.env.example` a `.env` y edita las credenciales:

```bash
cp .env.example .env
```

```env
DB_USER=papervault
DB_PASSWORD=tu_contraseña_segura
DB_NAME=papervault
DATABASE_URL=postgresql+psycopg://papervault:tu_contraseña_segura@localhost:15432/papervault
SESSION_SECRET=clave_aleatoria_de_al_menos_32_caracteres
SESSION_HTTPS=0
```

`SESSION_SECRET` firma las cookies de sesión y es obligatoria: la aplicación no arranca sin ella. Genérala con:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Pon `SESSION_HTTPS=1` solo si la aplicación se sirve por HTTPS (activa la cookie `Secure` y HSTS).

### 2. Base de datos

```bash
docker compose up -d
```

Para detener sin eliminar datos:

```bash
docker compose stop
```

### 3. Dependencias Python

```bash
python -m venv venv
venv\Scripts\activate          # Windows
pip install -r requirements.txt
```

### 4. Ejecutar

```bash
uvicorn main:app --reload
```

Accede a [http://localhost:8000](http://localhost:8000).

En el primer arranque se crea el usuario `admin` con una contraseña temporal que se muestra en la consola. Se pedirá cambiarla al ingresar.

> **Nota:** PaddleOCR descarga los modelos de red neuronal la primera vez que se utiliza el motor OCR. Esto puede tardar varios minutos dependiendo de la conexión. Los arranques posteriores son inmediatos.

## Estructura del proyecto

```
PaperVault/
├── main.py                  # Aplicación FastAPI: rutas, autenticación, lógica principal
├── core/
│   ├── database.py          # Modelos SQLAlchemy y función de migración incremental
│   └── ocr_engine.py        # Motor OCR: PaddleOCR, procesamiento de zonas, extracción de texto
├── templates/
│   ├── index.html           # Dashboard principal (SPA con JS)
│   ├── login.html           # Página de inicio de sesión
│   └── cambiar_password.html
├── static/
│   └── logo.png
├── uploads/                 # Archivos subidos (generado automáticamente)
│   ├── referencias/         # Imágenes de referencia para plantillas OCR
│   └── tmp/                 # Archivos temporales del flujo preview/confirmar
├── docs/                    # Documentación técnica y diagramas UML
├── docker-compose.yml
├── .env.example
└── requirements.txt
```

## Formatos soportados

Imágenes: `.png`, `.jpg`, `.jpeg`, `.tiff`, `.tif`, `.bmp`  
Documentos: `.pdf`

## Endpoints principales

| Método | Ruta | Descripción |
|--------|------|-------------|
| `GET` | `/` | Dashboard principal |
| `POST` | `/subir` | Carga y procesa un documento con OCR completo |
| `GET` | `/ver/{id}` | Sirve el archivo original inline (visor) |
| `GET` | `/archivo/{id}` | Descarga el archivo original |
| `DELETE` | `/eliminar/{id}` | Elimina un documento |
| `GET` | `/api/documentos` | Lista documentos (con búsqueda opcional) |
| `GET` | `/api/plantillas` | Lista plantillas OCR |
| `POST` | `/api/plantillas` | Crea una plantilla |
| `PUT` | `/api/plantillas/{id}/zonas` | Guarda las zonas de una plantilla |
| `POST` | `/api/plantillas/{id}/preview` | Extrae campos sin guardar |
| `POST` | `/api/plantillas/confirmar` | Guarda el documento tras el preview |
| `GET/POST` | `/login` `/logout` | Autenticación |
| `GET/POST` | `/api/usuarios` | Gestión de usuarios (admin) |
