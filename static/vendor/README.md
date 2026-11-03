# Recursos de terceros

Se sirven desde `/static/vendor/` en lugar de un CDN, para que la CSP solo admita el propio origen y la aplicación funcione sin acceso a internet. Cada carpeta incluye la licencia del proyecto.

| Recurso | Versión | Origen | Licencia |
|---------|---------|--------|----------|
| `chartjs/chart.umd.min.js` | Chart.js 4.6.1 | `https://cdn.jsdelivr.net/npm/chart.js@4.6.1/dist/chart.umd.min.js` | MIT |
| `bootstrap-icons/` | Bootstrap Icons 1.11.3 | `https://cdn.jsdelivr.net/npm/bootstrap-icons@1.11.3/font/` | MIT |
| `inter/*.woff2` | Inter (@fontsource-variable/inter 5.3.0), subconjuntos latin y latin-ext | `https://cdn.jsdelivr.net/npm/@fontsource-variable/inter@5.3.0/files/` | OFL-1.1 |

`inter/inter.css` es propio: declara la fuente con el nombre `'Inter'` que usan las plantillas.

Para actualizar un recurso, descarga la nueva versión desde la misma ruta cambiando el número de versión, verifica su hash con `https://data.jsdelivr.com/v1/packages/npm/<paquete>@<versión>?structure=flat` y actualiza esta tabla.
