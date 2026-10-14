# SirWattson

Limitador de carga para un **Tesla Model 3** que evita superar la **potencia contratada de 4,6 kW (20 A)** de la vivienda. Un medidor **Shelly EM** con pinza en la acometida general mide el consumo total; un programa en Python decide cuántos amperios (5-32 A) puede usar el coche y se los ordena a un **ESP32** que habla con el Tesla por Bluetooth Low Energy.

> **Estado del proyecto (octubre 2026):** el controlador, el simulador y los tests funcionan sin hardware. Falta flashear el ESP32, emparejarlo con el coche, recibir el Shelly y escribir la conexión real con el ESP32 (`EsphomeCar`). Ver [Estado y pendientes](#estado-y-pendientes).

> **Aviso de seguridad:** esto es una ayuda de gestión de carga, **no un dispositivo de protección**. No sustituye al ICP/limitador de la compañía ni al magnetotérmico. Un fallo del software no debe poder causar un riesgo eléctrico; por eso el diseño falla hacia "cargar menos" (ver [Modo seguro](#modo-seguro)).

---

## Índice

1. [Arquitectura](#arquitectura)
2. [Decisiones y conclusiones](#decisiones-y-conclusiones)
3. [Algoritmo de control](#algoritmo-de-control)
4. [Estructura del repositorio](#estructura-del-repositorio)
5. [Guía para principiantes (desde cero)](#guía-para-principiantes-desde-cero)
6. [Estado y pendientes](#estado-y-pendientes)
7. [Posibles mejoras](#posibles-mejoras)
8. [Enlaces](#enlaces)

---

## Arquitectura

```text
                 acometida general (230 V, monofásica)
                          |
                 [pinza 50 A] --> Shelly EM (Gen1) --Wi-Fi--+
                                                            |  HTTP GET /emeter/0 (cada 5 s)
                                                            v
  +-------------------------------------------------------------+
  |  SirWattson (este repo, Python, corre en un Mac/Raspberry)  |
  |   shelly.py  ->  Controller (puro)  ->  Car (interfaz)      |
  +-------------------------------------------------------------+
                                   | API nativa de ESPHome (aioesphomeapi), Wi-Fi
                                   v
                       ESP32 "Elegoo" (ESP32 clásico)
                       firmware yoziru/esphome-tesla-ble
                                   | Bluetooth Low Energy
                                   v
                              Tesla Model 3
```

**Piezas:**

| Pieza | Función |
|---|---|
| Shelly EM (Gen1) + pinza 50 A | Mide la potencia total de la casa (incluye el coche, porque la pinza está en la acometida). |
| ESP32 clásico (Elegoo, chip USB CP2102) | Ejecuta el firmware de terceros que habla BLE con el coche: fijar amperios, iniciar/parar carga, leer estado. |
| SirWattson (Python) | Lee el Shelly, calcula el amperaje permitido y lo manda al ESP32. No usa Home Assistant. |
| Simulador (`sim.py`, `sim_server.py`) | Imita el Shelly y un Model 3 para probar sin hardware. |

**Principio de diseño:** `controller.py` es una función pura (tiempo + lectura -> orden). No hace red ni lee relojes, así que se prueba con tests deterministas. Todo lo que toca el mundo exterior (HTTP al Shelly, ESP32, simulador) está detrás de interfaces pequeñas y se intercambia sin tocar el algoritmo.

---

## Decisiones y conclusiones

### 1. Shelly EM Gen1 en lugar de Gen3

Se compró el **Gen1 (37 €)** frente al Gen3 (60 €). Razonamiento:

- Para un "lector de potencia" basta. Mide potencia, tensión y un indicador `is_valid`, y se consulta con un `GET` simple.
- El Gen3 sí mide tensión y es una plataforma activa (API RPC, Bluetooth, scripts), mientras que la rama de firmware Gen1 está estancada en la 1.14.0 de 2023 y solo recibe correcciones. Ese es el único argumento sólido a favor del Gen3, y se aceptó el riesgo.
- El historial interno (365 días en Gen1, 10 en Gen3) **no importa**: el algoritmo decide cada pocos segundos y los datos históricos, si se quieren, se guardan fuera.
- **Corrección de un error previo:** se afirmó que el Gen3 "asume la tensión"; no era correcto. Esa idea venía de un mensaje de foro sobre un montaje de 240 V en EE. UU. y no de la documentación del dispositivo.

### 2. El Shelly no puede hablar con el Tesla

Se estudió programar el Gen3 con scripts para eliminar el ESP32. **No es viable:**

- Los scripts de Shelly solo escanean anuncios BLE; no se ha encontrado ninguna función para abrir una conexión GATT y escribir en el coche.
- Los scripts tienen unos 25 KB de memoria (código + ejecución) e intérprete tipo ES5; el protocolo del Tesla exige criptografía de curva elíptica y mensajes protobuf.
- El hardware admite un único escaneo BLE a la vez.

Conclusión: **el ESP32 es imprescindible** para el BLE, y el Shelly solo mide.

### 3. Un único "cerebro" fuera del ESP32, sin Home Assistant

Se quiere implementar el algoritmo sin Home Assistant. El ESP32 con el firmware de yoziru expone sus controles como entidades de ESPHome, accesibles por **API nativa** o por **REST (`web_server`)**.

| | REST (`web_server`) | API nativa (`aioesphomeapi`) |
|---|---|---|
| Prueba rápida | `curl` | Requiere script Python |
| Estado | Hay que sondear | Suscripción a cambios |
| Seguridad | Sin autenticación por defecto; expone el control del coche en la LAN | Cifrado con clave (`noise_psk`) |

**Decisión:** API nativa con `aioesphomeapi`. REST queda descartado como vía principal por la seguridad.

### 4. Python 3.11+ con asyncio

Hay librería oficial (`aioesphomeapi`) para el protocolo del ESP32, el servidor HTTP del simulador usa `aiohttp`, y el controlador solo depende de la biblioteca estándar. Eso permite ejecutar los tests aunque no haya red ni dependencias instaladas.

### 5. Elección de placa ESP32

- El repositorio de yoziru da soporte a **M5Stack NanoC6, AtomS3 y ESP32 genérico (clásico `esp32dev`)**.
- Se usa un **Elegoo ESP32 clásico**, así que el paquete correcto es `esp32-generic`. No usar el paquete genérico con variantes C3/S3.
- Se evaluó la **Seeed XIAO ESP32-C6**: es el mismo chip que la NanoC6 pero exigiría crear un archivo de placa propio; su ventaja real sería la antena externa. El enlace de Amazon que se compartió parecía ser la **XIAO ESP32-S3 Sense**, que no es recomendable aquí. Zigbee/Thread/AWS/Azure no aportan nada a este proyecto.

### 6. Dónde va la pinza

**En la acometida general**, rodeando **un solo conductor** (no fase y neutro juntos; se anularía). Así el Shelly ve el total de la casa, y el controlador reduce el coche cuando el total se acerca al límite. Si la potencia sale negativa, la pinza está al revés.

### 7. Límites elegidos

- Potencia contratada: 4.600 W (20 A a 230 V). Se asume **red monofásica de 230 V**.
- **Techo operativo: 4.370 W (19 A)**, dejando ~230 W de margen porque otros consumos llegan con retardo.
- Rango del cargador: **5-32 A**.

### 8. Correcciones al diseño inicial del algoritmo

- **Umbral de reinicio:** la primera propuesta era reiniciar por debajo de 3,91 kW, pero a 5 A el coche suma 1.150 W y se superaría la potencia contratada. Ahora se reinicia solo si *casa + 5 A* cabe bajo el techo (casa < 3.220 W). Hay un test.
- **Bajada proporcional:** en lugar de bajar 1 o 3 A fijos, se calcula cuántos amperios sobran con \( \lceil (P - 4370) / 230 \rceil \). Así un pico grande se corrige en un solo paso.

---

## Algoritmo de control

Se ejecuta cada 5 s con la lectura del Shelly (`power`, `is_valid`).

| Situación | Acción |
|---|---|
| Cargando y potencia > 4.370 W | Baja \( \lceil (P-4370)/230 \rceil \) A, mínimo 5 A |
| Ya a 5 A y potencia > 4.600 W | Detiene la carga |
| Cargando y potencia <= 4.140 W (4.370 - 230) | Sube 1 A como máximo cada 15 s, hasta 32 A |
| Entre 4.140 y 4.370 W | Mantiene |
| Detenido y (casa + 5 A) <= 4.370 W durante 60 s | Reinicia a 5 A |
| Lectura inválida o Shelly sin respuesta | Baja a 5 A; si dura 30 s, detiene la carga |

### Modo seguro

Si el Shelly no responde o reporta `is_valid: false`, el controlador **no sigue ordenando potencia a ciegas**: baja a 5 A y, pasados 30 s, para. El campo `is_valid` del Gen1 se trata como "no usar esta lectura".

---

## Estructura del repositorio

```text
SirWattson/
├── sirwattson/
│   ├── controller.py    # Lógica pura de control (sin E/S)
│   ├── sim.py           # Modelo del Model 3 y de la casa
│   ├── sim_server.py    # Shelly Gen1 falso + endpoint que hace de ESP32
│   ├── car.py           # SimHttpCar (simulado) y EsphomeCar (pendiente)
│   └── main.py          # Bucle: lee Shelly -> decide -> manda al coche
├── tests/test_controller.py
├── secrets.example.yaml # Plantilla de secretos (el real está en .gitignore)
├── pyproject.toml
└── README.md
```

---

## Guía para principiantes (desde cero)

Supone un **Mac** y ningún conocimiento previo. Los comandos se escriben en la app **Terminal** (Spotlight: pulsa `Cmd + Espacio`, escribe "Terminal", Enter). Copia cada bloque tal cual y pulsa Enter.

### Paso 0. Qué necesitas

- Un Mac con conexión a internet.
- El **ESP32** (Elegoo, clásico) y un **cable USB de datos** (muchos cables son solo de carga y no funcionan).
- Tu **Tesla Model 3** y su **tarjeta NFC**.
- El **Shelly EM Gen1** con pinza (para la parte real; no es necesario para el simulador).
- Un móvil **Android** (recomendado) para obtener la dirección Bluetooth del coche.

### Paso 1. Instalar las herramientas

1. **Homebrew** (instalador de programas para Mac):
   ```bash
   /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
   ```
   Al terminar, sigue las instrucciones "Next steps" que imprime para añadirlo al PATH.
2. **Git y uv** (uv gestiona Python y las dependencias):
   ```bash
   brew install git uv
   ```
3. Comprueba:
   ```bash
   git --version
   uv --version
   ```

### Paso 2. Descargar los proyectos

```bash
mkdir -p ~/github && cd ~/github
git clone git@github.com:jaimeagudo/sirwattson.git SirWattson
git clone https://github.com/yoziru/esphome-tesla-ble.git
```

Si `git clone` por SSH falla, usa la dirección HTTPS: `https://github.com/jaimeagudo/sirwattson.git`.

### Paso 3. Probar el controlador (sin hardware)

```bash
cd ~/github/SirWattson
uv sync --python 3.13
python3 -m unittest discover -s tests -v
```

Deben pasar los **6 tests**. Los tests no necesitan instalar nada extra, solo `python3`.

> **Nota:** el desarrollo se hizo con Python 3.14 del sistema, pero no está comprobado que `esphome` y `aioesphomeapi` lo soporten. Por eso se recomienda `--python 3.13`.

### Paso 4. Ejecutar el simulador

Abre **dos ventanas de Terminal**.

**Terminal 1** (Shelly y coche falsos):
```bash
cd ~/github/SirWattson
uv run python -m sirwattson.sim_server
```

**Terminal 2** (el controlador):
```bash
cd ~/github/SirWattson
uv run python -m sirwattson.main
```

Verás cómo el controlador arranca la carga a 5 A tras 60 s y sube 1 A cada 15 s. Pruebas desde una tercera ventana:

```bash
# Estado actual de la simulación
curl localhost:8081/_sim/state

# Simular un horno: la casa pasa a consumir 3.500 W
curl -X POST "localhost:8081/_sim/base?w=3500"

# Volver a la normalidad
curl -X POST "localhost:8081/_sim/base?w=650"

# Simular que el Shelly da lecturas inválidas
curl -X POST "localhost:8081/_sim/shelly?valid=0"

# Simular que el Shelly se apaga
curl -X POST "localhost:8081/_sim/shelly?online=0"
```

Para parar cualquier programa: `Ctrl + C`.

### Paso 5. Configurar los secretos

El archivo `secrets.yaml` guarda tus datos privados y **está en `.gitignore`: nunca se sube a GitHub**. Hay una plantilla:

```bash
cd ~/github/SirWattson
cp secrets.example.yaml secrets.yaml   # solo si aún no existe
open -e secrets.yaml                   # lo abre en TextEdit
```

Rellena:

| Clave | Qué es |
|---|---|
| `wifi_ssid` | Nombre de tu Wi-Fi (el ESP32 solo soporta **2,4 GHz**) |
| `wifi_password` | Contraseña de la Wi-Fi |
| `tesla_vin` | VIN del coche (17 caracteres, en la ficha o en la app Tesla) |
| `tesla_ble_mac_address` | Dirección Bluetooth del coche (ver Paso 6) |

### Paso 6. Obtener la dirección Bluetooth (MAC) del coche

El coche emite constantemente un anuncio BLE con un nombre derivado del VIN, sin necesidad de despertarlo.

**Opción A (recomendada): móvil Android**
1. Instala la app **nRF Connect**.
2. Acércate al coche y escanea.
3. Busca un dispositivo cuyo nombre tenga **18 caracteres, empiece por `S` y acabe en `C`** (por ejemplo `S1a87a5a75f3df858C`).
4. Copia su dirección (formato `A0:B1:C2:D3:E4:F5`) en `tesla_ble_mac_address`.

**Opción B: sin Android.** iOS no muestra la MAC. El firmware incluye un modo de escucha temporal: hay que descomentar `listener: !include listener.yml` en `packages/base.yml` del proyecto `esphome-tesla-ble`, compilar, flashear y leer la MAC en el registro (`make logs`); luego se vuelve a comentar la línea y se hace `make clean`.

### Paso 7. Flashear el ESP32

1. Conecta el ESP32 al Mac con el cable USB de datos.
2. Comprueba que el Mac lo ve:
   ```bash
   ls /dev/cu.* | grep -i usb
   ```
   Debe aparecer algo como `/dev/cu.usbserial-0001`. Si **no** aparece nada: prueba otro cable y otro puerto; si sigue sin aparecer, instala el driver **CP210x** de Silicon Labs (el chip USB del Elegoo es un CP2102).
3. Copia los secretos al proyecto del firmware. **Revisa antes** el archivo `secrets.yaml.example` de `esphome-tesla-ble`, porque los nombres de las claves del Wi-Fi deben coincidir con los que espera ese proyecto:
   ```bash
   cd ~/github/esphome-tesla-ble
   cat secrets.yaml.example
   cp ~/github/SirWattson/secrets.yaml secrets.yaml
   ```
   Si los nombres no coinciden, edita el `secrets.yaml` copiado.
4. Valida, compila y flashea (la primera compilación descarga herramientas y tarda varios minutos):
   ```bash
   make validate-config BOARD=esp32-generic
   make compile BOARD=esp32-generic
   make upload BOARD=esp32-generic
   make logs
   ```
   Si el envío termina al 100 % con un error, la placa no coincide con el chip; comprueba que usas `esp32-generic`.

### Paso 8. Emparejar el ESP32 con el coche

1. Siéntate en el coche con el ESP32 encendido y dentro de alcance.
2. Inicia el emparejamiento ("Pair BLE key") y **apoya inmediatamente la tarjeta NFC en la consola central**.
3. Confirma el aviso en la pantalla del coche.
4. Verifica en *Controles > Cerraduras* que aparece una llave nueva ("Unknown device"); puedes renombrarla.

El rol de la llave determina lo que puede hacer: `DRIVER` (acceso completo, por defecto) o `CHARGING_MANAGER` (solo carga). Para este proyecto `CHARGING_MANAGER` sería suficiente y más seguro; cambiarlo exige volver a emparejar.

> **Pendiente de verificar:** la documentación del firmware describe el botón "Pair BLE key" dentro de Home Assistant. Aún no se ha comprobado cómo pulsarlo **sin** Home Assistant (por ejemplo, con `aioesphomeapi` o con el servidor web de ESPHome). Hasta entonces, el emparejamiento puede hacerse una única vez con un Home Assistant temporal.

### Paso 9. Instalar y configurar el Shelly EM (cuando llegue)

1. Alimenta el Shelly y conéctate al punto de acceso Wi-Fi `shellyem-XXXXXXXXXXXX`.
2. Abre `http://192.168.33.1/` y configura tu Wi-Fi.
3. Reserva una **IP fija** para el Shelly en tu router.
4. Actualiza el firmware y, si quieres, activa la autenticación HTTP.
5. Instala la pinza en la acometida, rodeando **un solo conductor**.
6. Comprueba la lectura:
   ```bash
   curl http://IP_DEL_SHELLY/shelly
   curl http://IP_DEL_SHELLY/emeter/0
   ```
   `/emeter/0` debe devolver `power`, `voltage` e `is_valid`.
7. Verifica con un aparato de potencia conocida (un radiador) que `power` es correcto y positivo.

> Instalar una pinza en el cuadro eléctrico implica manipular la instalación. Si no estás seguro, **que lo haga un electricista**.

### Paso 10. Ejecutar con el hardware real

```bash
cd ~/github/SirWattson
uv run python -m sirwattson.main --shelly-url http://IP_DEL_SHELLY --car esphome \
    --esphome-host IP_DEL_ESP32 --esphome-psk CLAVE_DE_CIFRADO
```

> `--car esphome` **aún no funciona**: `EsphomeCar` es un esqueleto que lanza `NotImplementedError`. Los nombres de las entidades del firmware se conocen solo tras el primer flasheo (ver [pendientes](#estado-y-pendientes)).

### Problemas frecuentes

| Síntoma | Causa probable |
|---|---|
| `ls /dev/cu.*` no muestra el ESP32 | Cable solo de carga, puerto defectuoso o falta el driver CP210x |
| `uv sync` falla con "Operation not permitted" o error de red | Sin internet o ruta de caché sin permisos; prueba `UV_CACHE_DIR=/tmp/uv-cache uv sync` |
| El emparejamiento falla con error HMAC | MAC o VIN incorrectos en `secrets.yaml` |
| "Found Tesla vehicle" nunca aparece | La MAC no es la del anuncio BLE correcto |
| `Finishing update failed` al flashear | Se usó el paquete de otra placa; usa `esp32-generic` |
| El controlador baja a 5 A y para | El Shelly no responde o devuelve `is_valid: false` (comportamiento esperado) |

---

## Estado y pendientes

**Hecho**
- Controlador con tests (rampa, otros consumos, picos, reinicio, fallo del Shelly).
- Simulador de Shelly Gen1 y de Model 3 con servidor HTTP.
- Bucle principal y esqueleto del coche real.
- Detección del ESP32 en USB (CP2102).

**No hecho / sin verificar**
- Flashear el firmware (no se pudo compilar desde el entorno de desarrollo por no tener red).
- Escribir `EsphomeCar` con `aioesphomeapi` (faltan los nombres reales de las entidades).
- Emparejamiento sin Home Assistant (ver Paso 8).
- `sim_server.py` y `main.py` **no se han ejecutado** todavía; solo se han probado el controlador y el simulador con unittest.
- **Velocidad de refresco de la potencia del Shelly Gen1:** no se ha encontrado el dato; se medirá con `curl` cada segundo. Determina cuánto se puede acortar el ciclo de 5 s.
- Si `esphome` y `aioesphomeapi` funcionan con Python 3.14.
- Cuántos clientes simultáneos admite la API nativa del firmware.
- Que la instalación sea realmente **monofásica a 230 V** y que el consumo máximo previsto de la casa quede dentro del rango de la pinza de 50 A (mide hasta ~50 A con precisión, satura cerca de 85 A).
- La pinza incluida en el Shelly Gen1 comprado (existen versiones de 50 A y de 120 A).

---

## Posibles mejoras

- **Excedente solar:** modo que ajuste los amperios al sobrante fotovoltaico, además del límite de potencia.
- **Histórico y gráficas:** registrar potencia, amperios y estado (CSV, Prometheus o Grafana) para ajustar umbrales.
- **Ejecución como servicio:** `launchd` en macOS o `systemd` en una Raspberry Pi, con reinicio automático y logs.
- **Despliegue en una Raspberry Pi o NAS** encendido 24 h en lugar de un Mac.
- **Contenedor Docker** para reproducir el entorno.
- **Tensión real:** usar la tensión que reporta el Shelly en lugar de los 230 V fijos.
- **Instalaciones trifásicas:** extender el modelo a tres fases (Shelly 3EM).
- **Migrar a Shelly EM Gen3** si interesa una plataforma con firmware activo.
- **Persistencia del estado** para retomar tras un reinicio sin detener la carga.
- **Calibración automática** del margen de seguridad con datos reales.
- **Tarifas horarias:** programar la carga en las horas valle.
- **Notificaciones** (Telegram, correo) ante fallos del Shelly o del ESP32.
- **Límite de batería** y horario de salida configurables.
- **Tests de integración** con `aioesphomeapi` contra un ESP32 simulado.
- **Antena externa** o placa alternativa (XIAO ESP32-C6, NanoC6) si la cobertura BLE en el garaje es mala.

---

## Enlaces

**Proyecto del que depende (firmware del ESP32)**
- [yoziru/esphome-tesla-ble](https://github.com/yoziru/esphome-tesla-ble): firmware ESPHome para controlar un Tesla por BLE.
- [PedroKTFC/esphome-tesla-ble](https://github.com/PedroKTFC/esphome-tesla-ble): fork con variantes, útil como referencia.

**Shelly**
- [API Gen1](https://shelly-api-docs.shelly.cloud/gen1/): referencia REST del Shelly EM original.
- [Shelly EM (Gen1)](https://kb.shelly.cloud/knowledge-base/shelly-em) y [Shelly EM Gen3](https://kb.shelly.cloud/knowledge-base/shelly-em-gen3) en la base de conocimiento oficial.
- [Historial de firmware](https://shelly.wiki/en/firmware-versions/): estado de las ramas Gen1 y Gen2/3.

**ESPHome y Python**
- [aioesphomeapi](https://github.com/esphome/aioesphomeapi): cliente Python de la API nativa de ESPHome.
- [API nativa de ESPHome](https://esphome.io/components/api/) y [API web/REST](https://esphome.io/web-api/).
- [Seeed XIAO ESP32-C6 para ESPHome](https://github.com/DerekSeaman/ESPHome-Seeed-Xiao-ESP32-C6-Config): configuración de referencia si se cambia de placa.
- [uv](https://docs.astral.sh/uv/): gestor de entornos y dependencias de Python.
