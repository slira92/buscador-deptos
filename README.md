# Buscador Deptos Arriendo

Scraper personal de Portal Inmobiliario que corre solo mientras trabajas y te avisa de los
departamentos que calzan con tu búsqueda.

**Búsqueda actual** (se edita en `config.json`):
- Arriendo de departamento en **Las Condes** y **Providencia oriente** (no más al poniente de
  Av. Los Leones; quedan dentro Plaza Las Lilas y El Bosque).
- **2–3 dormitorios**, **2+ baños**.
- **Sin edificios multifamily** (edificios de renta con varias unidades y precio "desde").
- **Total ≤ $1.400.000** = arriendo + gastos comunes. Los avisos en UF se pasan a pesos con la UF del día.

## Cómo funciona

1. Recorre la búsqueda filtrada de cada comuna (precio, dormitorios y baños ya filtrados en la URL).
2. Entra a la ficha de cada aviso **nuevo** para leer gastos comunes, coordenadas, m², estacionamiento,
   bodega, mascotas, piso, orientación, antigüedad, fotos y descripción. Los avisos ya conocidos
   solo se vuelven a leer si cambian de precio o cada 7 días.
3. Calcula el total mensual. Si el aviso no informa GGCC, los estima en $3.000/m² y los marca como "est.".
4. Aplica el límite de zona con las coordenadas del aviso.
5. Guarda todo en `data/avisos.json`, regenera `data/datos.js` y avisa:
   - **Notificación de Windows** (clic → abre el dashboard) cuando aparecen avisos nuevos o bajan de precio.
   - **Correo** con fotos y datos, si lo activas (ver abajo).

La primera ejecución carga todo el stock (~30–40 min, porque lee cada ficha con pausas para no
saturar el portal). Las siguientes tardan 1–3 min.

## Dashboard

Abre `dashboard.html` en el navegador. Se actualiza solo cada 2 minutos sin perder tus filtros.

- Filtros: total máximo (slider), dormitorios, comuna, con estacionamiento, mascotas, multifamily,
  nuevos 48 h, favoritos, fuera de zona, descartados, texto libre.
- Cada depto aparece una vez: si está publicado varias veces, muestra el más barato y "+N publicaciones iguales".
- ★ favoritos, ✕ descartar y notas por aviso (quedan guardados en tu navegador).
- Mapa con todos los avisos y la línea del límite poniente de Providencia.
- Vista tabla para comparar.

## Análisis UF/m² y puntaje

La vista **Análisis** (por defecto) muestra:

- **Top 10** según un puntaje 0–100 con estos pesos (editables en `config.json` → `pesos_puntaje`):
  - Precio vs mercado (30%): UF/m² del arriendo contra la mediana de comparables a ≤1 km con los
    mismos dormitorios (se amplía a 1,5–2 km si hay menos de 5). -25% = 100 pts, +25% = 0.
  - Costo total (15%): arriendo + GGCC; 65% del presupuesto o menos = 100 pts.
  - Superficie (20%): 2D de 55 a 85 m², 3D de 70 a 110 m².
  - Ubicación (20%): distancia a la estación de Metro más cercana; ≤300 m = 100, ≥1.500 m = 0.
  - Extras (15%): estacionamiento 50, bodega 20, antigüedad ≤15 años 30.
- **Gráfico UF/m² vs m²** por comuna, con la mediana y el top 10 marcado.
- **Tabla por sector** (Metro más cercano): mediana UF/m², total, m² y mejor opción.

Los avisos en pesos se pasan a UF con la UF del día. Si un aviso tiene m² o precio fuera de rango
(menos de 30 o más de 250 m², o menos de 0,15 o más de 1,2 UF/m²) se marca "⚠ revisar"
y no cuenta como comparable.

Ojo con el sesgo: el "mercado" se arma con los avisos que trae la búsqueda (arriendo ≤ $1.400.000),
así que en deptos grandes la mediana sale más baja que el mercado completo.

## Comandos

```bash
py buscador.py                    # ejecución normal
py buscador.py --max-paginas 1    # prueba rápida
py buscador.py --solo-dashboard   # recalcula con config.json sin scrapear (ej: cambiaste presupuesto o límite)
py buscador.py --probar-aviso     # manda una notificación (y correo, si está activo) de prueba
```

## Link para compartir (GitHub Pages)

Después de cada pasada el buscador sube el dashboard a https://slira92.github.io/buscador-deptos/
(rama `gh-pages`, un solo commit que se reescribe). Se configura en `config.json` → `publicar_github`.

Por qué no corre en la nube: Mercado Libre (dueño de Portal Inmobiliario) manda las consultas que
vienen de servidores (GitHub, AWS, etc.) a una verificación de cuenta, y su API oficial exige una
app de desarrollador autorizada. Desde una conexión de casa u oficina funciona normal; por eso
la búsqueda la hace el PC y la nube solo publica.

## Ejecución automática en el PC

```bash
powershell -ExecutionPolicy Bypass -File instalar_tarea.ps1                 # todos los días cada 30 min, 08:00–22:00
powershell -ExecutionPolicy Bypass -File instalar_tarea.ps1 -SoloSemana     # solo L-V
powershell -ExecutionPolicy Bypass -File instalar_tarea.ps1 -CadaMinutos 15 # más seguido
powershell -ExecutionPolicy Bypass -File instalar_tarea.ps1 -Desinstalar    # quitarla
```

Corre con tu sesión de Windows iniciada. Si el PC está **suspendido** lo despierta para correr
(requiere que el plan de energía permita "temporizadores de reactivación"); si está **apagado**
no corre, y al prenderlo hace la pasada pendiente.

## Activar correo en el PC (Gmail)

1. Crea una contraseña de aplicación en https://myaccount.google.com/apppasswords.
2. Copia `.env.example` como `.env` y completa `SMTP_USUARIO`, `SMTP_PASSWORD` y `CORREO_DESTINO`.
3. Prueba con `py buscador.py --probar-aviso`.

Ojo: esta carpeta está en OneDrive, así que el `.env` se sincroniza (nunca se sube a GitHub).

## Ajustes útiles en `config.json`

| Clave | Para qué |
|---|---|
| `busqueda.presupuesto_total_clp` | Tope arriendo + GGCC |
| `busqueda.dormitorios_min/max`, `banos_min` | Programa |
| `limites_por_comuna.Providencia.lon_min` | Límite poniente. `-70.604` ≈ Los Leones; `-70.598` ≈ solo Las Lilas/Tobalaba |
| `busqueda.incluir_edificios_multifamily` | Edificios de renta (precio "desde", varias unidades) |
| `ggcc_estimado_por_m2_clp` | Estimación cuando el aviso no informa GGCC |
| `pausa_entre_requests_seg` | Pausa entre consultas (no bajar de 1 s) |

## Si algo falla

- Los registros quedan en `logs/`.
- Si el portal pide captcha o bloquea, el buscador se detiene, avisa y reintenta en la siguiente pasada.
- Si Portal Inmobiliario cambia su página, lo más probable es que haya que ajustar `parsear_tarjeta`
  o `parsear_detalle` en `buscador.py`.
