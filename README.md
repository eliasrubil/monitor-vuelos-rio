# monitor-vuelos-rio

Monitor de precios de vuelos **Buenos Aires → Brasil (GIG, GRU, CFB)**, 100% determinístico (sin LLMs en
tiempo de ejecución). Corre una vez por día en GitHub Actions, guarda el historial en SQLite
(`data/prices.db`, commiteada por el workflow) y avisa por **Telegram** y **email** cuando detecta precios
bajos.

- Búsqueda: ida y vuelta al mismo destino, ida ≥ 2027-01-14, vuelta ≤ 2027-02-05, duración 9/10/11 días
  (39 pares de fechas por destino), 5 adultos, máximo 1 escala, precios en USD con impuestos.
- Fuente de datos: [Ignav](https://ignav.com/docs) (`POST /api/fares/round-trip`), detrás de una interfaz
  `PriceSource` intercambiable.
- Todo se configura en [`config.yaml`](config.yaml).

---

## Índice

1. [Cómo funciona](#cómo-funciona)
2. [Puesta en marcha paso a paso](#puesta-en-marcha-paso-a-paso)
   1. [Clave de Ignav](#1-clave-de-ignav)
   2. [Bot de Telegram y chat_id](#2-bot-de-telegram-y-chat_id)
   3. [Contraseña de aplicación de Gmail](#3-contraseña-de-aplicación-de-gmail)
   4. [Cargar los Secrets en GitHub](#4-cargar-los-secrets-en-github)
   5. [Activar el workflow](#5-activar-el-workflow)
   6. [Primera corrida](#6-primera-corrida)
3. [Correr localmente y tests](#correr-localmente-y-tests)
4. [Cambiar parámetros](#cambiar-parámetros)
5. [Presupuesto de requests y costo](#presupuesto-de-requests-y-costo)
6. [Base de datos](#base-de-datos)
7. [Limitaciones conocidas de la API](#limitaciones-conocidas-de-la-api)

---

## Cómo funciona

```
config.yaml ─┐
             ▼
  planner ──► PriceSource (Ignav) ──► SQLite ──► detección (A/B/C) ──► Telegram + email
  (qué consultar)  (timeouts, reintentos)        (+ anti-spam)            (+ resumen por email)
```

**Origen y destinos.** El origen es `BUE`, el código de ciudad de Buenos Aires: una sola consulta cubre EZE
y AEP, incluidas las combinaciones mixtas (ida desde EZE, vuelta a AEP). El aeropuerto real se lee de cada
tramo (`departure_airport` / `arrival_airport`) y se muestra en las alertas (`EZE`, `AEP` o `EZE/AEP` si ida
y vuelta usan aeropuertos distintos). Los destinos son **GIG**, **GRU** y **CFB**: para Río se usa solo GIG
(no el código de ciudad `RIO`, que incluye Santos Dumont) y para São Paulo solo GRU.
`search.airports_exclude: [SDU]` impide que Santos Dumont sea la salida o la llegada de la ida o de la vuelta
(nunca es destino). Como escala sí se acepta: BUE → SDU → GIG en la ida o GIG → SDU → BUE en la vuelta.

**Optimización de consultas** (por destino):

| Situación | Qué se consulta |
|---|---|
| Primera corrida, o `--full-scan` | Barrido completo: 39 pares × 2 orígenes |
| Corridas siguientes | `top_k` (8) pares más baratos + `rotating_k` (5) del resto, el dato más viejo primero |
| Garantía de frescura | Si un par llegaría a `max_staleness_runs` (6) corridas sin actualizar, se agrega igual |
| `no_service` | Una consulta que dio "sin resultados" 2 corridas seguidas se marca `no_service` y se reconsulta cada `recheck_days` (14) |
| GRU (modo reducido) | Se consulta cada `reduced_interval_days` (2). Pasa a seguimiento normal si su mediana por persona ≤ mediana de GIG − `gru_margin_usd` (50); si deja de cumplirlo, vuelve a reducido |

Ignav no ofrece búsqueda por calendario ni fechas flexibles (cada consulta es un par de fechas), así que la
optimización se hace eligiendo qué pares consultar.

**Detección** (umbrales en `config.yaml > detection`):

- **Regla A (temporal):** el itinerario contra su propio historial (mínimo 7 observaciones). Alerta si
  `precio_pp < media × (1 − 0.15)` o si el z robusto `(precio − mediana) / (1.4826 × MAD) < −2.5`.
  Si el MAD es 0 (precios idénticos), solo aplica la condición de la media.
- **Regla B (transversal):** el itinerario contra el último precio conocido de todos los pares del mismo
  destino (mínimo 10 pares). Alerta si está entre las **3 fechas más baratas** del destino (`max_rank`) **y** ≥ 15% debajo de la media.
- **Regla C (absoluta):** `absolute_threshold_usd_pp`, desactivada mientras sea `null`.
- Si se disparan varias reglas, se manda **una sola alerta**. La regla no se muestra en el mensaje, pero
  queda guardada en la tabla `alerts` y en el log de la corrida.
- **Anti-spam:** un itinerario ya alertado solo se vuelve a alertar si su precio baja ≥ 3% respecto de la
  última alerta.

**Alertas.** Telegram recibe **un solo mensaje por corrida** con todas las alertas en formato corto (si supera
el límite de caracteres de Telegram se parte en varios mensajes, sin cortar ninguna alerta):

```
AEP --> CFB 20/01 al 31/01 (11 días) - 834USD por persona - Aerolíneas Argentinas
Reserva: https://...
EZE --> GIG 16/01 al 26/01 (10 días) - 405USD por persona (+2 fechas más con el mismo precio) - JetSMART
Reserva Ida: https://...
Reserva Vuelta: https://...
```

El email recibe uno por corrida con todas las alertas en formato completo, más un **resumen** (top 5 más
baratos y media por destino) que se puede desactivar. Si varias fechas del mismo origen y destino alertan con
exactamente el mismo precio, se avisa solo la primera y se indica cuántas más tienen ese precio (todas quedan
registradas para el anti-spam). Si un canal
falla, se envía igual por el otro y se registra el error. Si ambos fallan, la alerta no queda registrada y se
vuelve a intentar en la próxima corrida. El link de reserva (`POST /api/fares/booking-links`) se pide **solo
para los itinerarios que disparan alerta**; se prefiere un link que cubra ida y vuelta y, si la fuente solo
ofrece links por tramo, se muestran por separado como "Ida" y "Vuelta".

**Aerolíneas excluidas.** `search.airlines_exclude` (por defecto `[FO]`, Flybondi) se envía a la API y además
se descarta cualquier itinerario con un tramo de esas aerolíneas, así que nunca pueden generar una alerta. Los
precios de Flybondi guardados antes de este cambio se borraron de la base con una migración automática.

Ejemplo de alerta:

```
Precio bajo EZE→GIG 15/01–25/01: USD 412 por persona

Fechas: vie 15/01/2027 → lun 25/01/2027 (10 días)
Escalas: 1
Aerolíneas Argentinas, GOL
Precio: USD 412 por persona · USD 2.060 total (5 adultos, con impuestos)
Reserva: https://...

>>STATS<<
Vs. itinerario: 17,6% debajo de la media (8 obs., media USD 500)
Vs. ventana GIG: 21,0% debajo de la media (puesto 1 de 39, media USD 522)
z robusto del itinerario: -5.40
```

Los nombres de las aerolíneas salen de `airline_names` en `config.yaml`; si aparece un código que no está
cargado, se muestra el código IATA.

**Robustez.** Cada consulta tiene timeout, hasta 3 reintentos con backoff exponencial (ante timeouts,
errores de conexión, 424, 429 y 5xx) y nunca frena la corrida. Los errores 401/402/403 (clave inválida,
facturación, email sin verificar) cortan las consultas restantes y mandan un aviso por los canales de alerta.

**Fin de campaña.** A partir del día siguiente a `schedule.stop_after` (2027-01-10), la corrida termina sin
consultar nada. Después de esa fecha podés desactivar el workflow (ver paso 5).

---

## Puesta en marcha paso a paso

### 1. Clave de Ignav

1. Creá una cuenta en <https://ignav.com/signup> y verificá el email (sin verificar, la API responde 403).
2. Copiá la API key desde <https://ignav.com/dashboard>.

Las primeras 1.000 requests son gratis, una sola vez. Después cuesta USD 2 cada 1.000 requests exitosas,
facturado por mes ([pricing](https://ignav.com/pricing)). Para pasar de las 1.000 gratis hay que cargar un
medio de pago en el dashboard. Si no, la API responde 402 y el monitor avisa.

### 2. Bot de Telegram y chat_id

1. En Telegram, abrí un chat con **@BotFather** y mandá `/newbot`.
2. Elegí un nombre y un usuario (tiene que terminar en `bot`, por ejemplo `vuelos_rio_bot`).
3. BotFather te devuelve el **token** (algo como `123456789:AAH...`). Ese es `TELEGRAM_BOT_TOKEN`.
4. Abrí el chat con tu bot nuevo y mandale cualquier mensaje (por ejemplo `hola`). Sin este paso el bot no
   puede escribirte.
5. Para obtener el **chat_id**, abrí en el navegador (reemplazando `<TOKEN>`):
   `https://api.telegram.org/bot<TOKEN>/getUpdates`
   y buscá `"chat":{"id":123456789,...}`. Ese número es `TELEGRAM_CHAT_ID`.
   - Para un **grupo**: agregá el bot al grupo, mandá un mensaje en el grupo y repetí `getUpdates`. El id del
     grupo es negativo (por ejemplo `-1001234567890`).
   - Si `getUpdates` devuelve `"result":[]`, mandale otro mensaje al bot y recargá.

### 3. Contraseña de aplicación de Gmail

Gmail no acepta tu contraseña normal por SMTP. Hace falta una **contraseña de aplicación**:

1. Activá la verificación en dos pasos en <https://myaccount.google.com/security> (es requisito).
2. Entrá a <https://myaccount.google.com/apppasswords>.
3. Escribí un nombre (por ejemplo `monitor-vuelos`) y tocá **Crear**.
4. Google muestra una contraseña de 16 letras. Ese valor es `GMAIL_APP_PASSWORD` (los espacios no importan).
   Solo se muestra una vez.
5. `GMAIL_ADDRESS` es la cuenta de Gmail que envía. `ALERT_EMAIL_TO` es quién recibe; puede ser la misma
   cuenta o varias separadas por coma (`a@gmail.com,b@gmail.com`).

### 4. Cargar los Secrets en GitHub

En el repo: **Settings → Secrets and variables → Actions → New repository secret**. Creá estos seis, con
estos nombres exactos:

| Secret | Valor |
|---|---|
| `IGNAV_API_KEY` | API key de Ignav |
| `TELEGRAM_BOT_TOKEN` | token de @BotFather |
| `TELEGRAM_CHAT_ID` | id del chat o grupo |
| `GMAIL_ADDRESS` | cuenta de Gmail que envía |
| `GMAIL_APP_PASSWORD` | contraseña de aplicación de 16 letras |
| `ALERT_EMAIL_TO` | destinatario(s) del email |

Las claves nunca se escriben en el código ni en `config.yaml`. El logger además reemplaza por `***`
cualquier valor secreto que aparezca en un mensaje de log.

### 5. Activar el workflow

1. Mergeá estos cambios a la rama principal. GitHub solo ejecuta los `schedule` de la rama por defecto.
2. Pestaña **Actions**: si GitHub muestra *"Workflows aren't being run on this repository"*, tocá
   **I understand my workflows, go ahead and enable them**.
3. El workflow **Monitor de vuelos** corre todos los días a las **11:00 UTC (08:00 en Argentina)**. GitHub
   puede demorar los cron unos minutos en horarios de mucha carga.
4. Necesita permiso de escritura para commitear la base. Ya está declarado (`permissions: contents: write`).
   Si tu organización lo restringe, habilitalo en **Settings → Actions → General → Workflow permissions →
   Read and write permissions**.
5. Para pausarlo (por ejemplo, después del 10/01/2027): **Actions → Monitor de vuelos → ⋯ → Disable
   workflow**.

`concurrency` evita que dos corridas se superpongan. El commit de la base lleva `[skip ci]`, así no dispara
los tests.

### 6. Primera corrida

En **Actions → Monitor de vuelos → Run workflow** hay tres opciones:

- **test_alert:** manda un mensaje de prueba por Telegram y email y termina. Hacelo primero para verificar
  los secrets.
- **dry_run:** hace como máximo `budget.dry_run_max_queries` (4) consultas reales, calcula todo y muestra
  los mensajes en el log, pero **no guarda precios ni envía alertas**. Como Ignav cobra esas requests,
  **se suman al contador de uso del mes**: el workflow registra la corrida (`runs.mode = 'dry_run'`) y las
  requests en la base, y la commitea.
- **demo_alert:** manda alertas simuladas (un email y un mensaje de Telegram) para ver cómo se ven, sin
  consultar la API ni tocar la base.
- **full_scan:** barrido completo de todas las combinaciones (117 requests).

La primera corrida normal ya hace el barrido completo sola (línea base). La regla A empieza a funcionar
cuando un itinerario acumula 7 observaciones.

---

## Correr localmente y tests

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt

# Tests: usan respuestas mockeadas de la API, nunca llaman a la API real ni envían mensajes
python -m pytest

# Exportar los secrets (o cargarlos desde un .env que NO se commitea)
export IGNAV_API_KEY=... TELEGRAM_BOT_TOKEN=... TELEGRAM_CHAT_ID=...
export GMAIL_ADDRESS=... GMAIL_APP_PASSWORD=... ALERT_EMAIL_TO=...

python -m monitor --test-alert       # mensaje de prueba por ambos canales
python -m monitor --dry-run -v       # hasta 4 consultas; no guarda precios ni alerta (sí cuenta el uso)
python -m monitor                    # corrida normal
python -m monitor --full-scan        # barrido completo
python -m monitor --config otra.yaml # otra configuración
```

Códigos de salida: `0` ok, `1` error fatal de la fuente o falla en el test de alertas, `2` configuración o
secrets faltantes.

---

## Cambiar parámetros

Editá [`config.yaml`](config.yaml) y commiteá; la próxima corrida lo toma. Las claves desconocidas
producen un error, así un typo no pasa desapercibido. Lo más común:

| Querés… | Cambiá |
|---|---|
| Otras fechas o duraciones | `search.departure_from`, `search.return_until`, `search.durations` |
| Agregar o sacar destinos u orígenes | `search.destinations`, `search.origins` (y `airport_names` para los textos) |
| Otra cantidad de pasajeros | `search.adults` (máximo 9 según la API) |
| Más o menos consultas por día | `optimization.top_k`, `optimization.rotating_k`, `optimization.max_staleness_runs` |
| Cambiar el tope mensual | `budget.max_requests_per_month` |
| Ajustar la sensibilidad | `detection.temporal.*`, `detection.cross.*` |
| Alerta por precio fijo | `detection.absolute_threshold_usd_pp: 350` (por persona, USD) |
| Sin resumen diario | `alerts.summary_email: false` |
| Sin links de reserva (ahorra requests) | `alerts.booking_links: false` |
| Apagar un canal | `alerts.telegram: false` o `alerts.email: false` |
| Otra fecha de fin | `schedule.stop_after` |
| Otro horario | el `cron` en `.github/workflows/monitor.yml` (está en UTC) |

`search.open_jaw` existe para habilitar a futuro combinaciones tipo ida GIG / vuelta CFB, pero el adaptador
de Ignav todavía no lo soporta (ver [limitaciones](#limitaciones-conocidas-de-la-api)). Si lo activás, se
loguea un aviso y se siguen consultando solo idas y vueltas al mismo destino.

### Agregar otra fuente de datos

Implementá `monitor.sources.base.PriceSource` (`search_round_trip` y, opcionalmente, `booking_link`),
registrala en `monitor/sources/__init__.py` y poné su nombre en `source:` de `config.yaml`. El resto (planner,
base, detección, alertas) no cambia.

---

## Presupuesto de requests y costo

Al iniciar, cada corrida loguea los requests estimados y el acumulado del mes. Si la corrida haría superar
`max_requests_per_month` (2500), **se saltea entera** y se avisa por Telegram y email. Al final se loguea
cuántas consultas se hicieron, cuántas se ahorraron frente a un barrido completo y el acumulado del mes. El
contador mensual cuenta solo las respuestas 200, que son las que Ignav factura, incluidas las de los
dry runs.

Con la configuración actual (medido con una simulación de 30 días):

| Corrida | Requests |
|---|---|
| Barrido inicial (39 pares × 3 destinos, origen BUE) | 117 |
| Día normal (GIG + CFB: 13 pares c/u) | 26 (27 si se fuerza un par viejo) |
| Día en que toca GRU (cada 2 días) | ~40 |
| Links de reserva | 1 por alerta |
| Dry run manual | hasta 4 |
| **Mes típico** | **~980** (el primer mes ~1.100 con el barrido) |

Costo: ~USD 2 por mes (USD 2 cada 1.000 requests).

---

## Base de datos

`data/prices.db` (SQLite). Se puede abrir con [DB Browser for SQLite](https://sqlitebrowser.org/) o con
`sqlite3`.

| Tabla | Contenido |
|---|---|
| `observations` | Mejor precio por consulta (origen + itinerario) y corrida: timestamp UTC, fuente, origen, destino, fechas, duración, escalas, aerolíneas, precio total, precio por persona, moneda, link de reserva |
| `pair_state` | Estado de cada consulta posible: último precio, fecha del dato, última consulta, racha sin resultados, `no_service` |
| `dest_state` | Modo de cada destino (normal/reducido) y contador de corridas |
| `runs` | Cada corrida: modo, consultas hechas y ahorradas, requests facturables, alertas |
| `requests` | Cada request HTTP a la fuente (base del contador mensual) |
| `alerts` | Alertas enviadas (para el anti-spam) |

Consultas útiles:

```sql
-- Últimos precios por itinerario
SELECT destination, depart_date, return_date, MIN(last_price_pp) AS pp, MAX(last_price_at) AS fecha
FROM pair_state WHERE no_service = 0 AND last_price_pp IS NOT NULL
GROUP BY destination, depart_date, return_date ORDER BY pp LIMIT 20;

-- Requests del mes
SELECT COUNT(*) FROM requests WHERE billable = 1 AND substr(ts_utc, 1, 7) = strftime('%Y-%m', 'now');
```

Si un par deja de devolver resultados, su último precio se borra de `pair_state` (deja de estar vigente),
aunque su historial queda en `observations`.

**Reinicio del 08/10/2026.** Al pasar a BUE se borraron los precios, estados y alertas anteriores (precios
corregidos a mano, tarifas de Flybondi y combinaciones EZE/AEP que ya no se usan) y se conservaron `runs` y
`requests`, para que el tope mensual siga contando lo consumido. La base anterior queda en el historial de git.

---

## Limitaciones conocidas de la API

Según la documentación de Ignav:

- **Códigos de ciudad:** la búsqueda acepta códigos de ciudad (`BUE`, `RIO`, `SAO`); la respuesta conserva el
  código pedido y cada tramo trae el aeropuerto real.
- **Sin calendario ni fechas flexibles:** cada request es un par de fechas.
- **Open-jaw:** Ignav tiene `POST /api/fares/search` (1 o 2 tramos con distintos aeropuertos), pero su
  esquema todavía no fue verificado, así que el adaptador no lo usa (`supports_open_jaw = False`).
- **Precio total:** `price.amount` es el total de todos los pasajeros (verificado en el
  [playground](https://ignav.com/playground)), así que `ignav.price_is_total: true` y el precio por persona
  es `amount / adults`.
- **Moneda:** se pide `market: US`. Las tarifas que no vengan en `USD` se descartan y se loguean.
- **Escalas:** se envía `max_stops: 1` y además se filtra localmente por la cantidad de segmentos de cada
  tramo.
- **Asientos:** la búsqueda es con 5 adultos juntos, así que una tarifa sin 5 asientos disponibles no
  aparece.
