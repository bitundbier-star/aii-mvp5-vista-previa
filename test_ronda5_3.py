"""Prueba manual de los 4 cambios de esta ronda (5.3) contra el servidor local."""
import datetime as dt
import io
import re

import requests
from openpyxl import load_workbook

BASE = "http://127.0.0.1:8123"
s = requests.Session()


def check(nombre, cond):
    print(("OK  " if cond else "FAIL"), nombre)
    assert cond, nombre


# Login con la cuenta demo
r = s.post(f"{BASE}/login", data={"login_email": "demo@aii.mx", "password": "demo1234"}, allow_redirects=True)
check("login demo", r.status_code == 200)

# ---------------------------------------------------------------------------
# 1. Excel: columna ID fija + Número editable, empareja por ID
# ---------------------------------------------------------------------------
r = s.get(f"{BASE}/propiedades/plantilla.xlsx")
check("plantilla descarga", r.status_code == 200 and len(r.content) > 1000)
wb = load_workbook(io.BytesIO(r.content))
ws = wb["Propiedades"]
encabezados = [ws.cell(row=1, column=i).value for i in range(1, 3)]
check("columna 1 es ID", encabezados[0] == "ID")
check("columna 2 es Número", encabezados[1] == "Número")

fila2_id = ws.cell(row=2, column=1).value
fila2_numero_original = ws.cell(row=2, column=2).value
check("ID de la fila 2 es un entero", isinstance(fila2_id, int))
print(f"   -> propiedad id={fila2_id}, numero original={fila2_numero_original!r}")

# Cambiamos el Número (columna 2) sin tocar el ID, tal como pediría Sofía
nuevo_numero = "ZZZ-9"
ws.cell(row=2, column=2).value = nuevo_numero
buf = io.BytesIO()
wb.save(buf)
buf.seek(0)

r = s.post(f"{BASE}/propiedades/importar", files={"archivo": ("plantilla.xlsx", buf, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
check("pantalla de confirmacion", r.status_code == 200 and "Así van a quedar" in r.text)
check("confirmacion muestra el numero nuevo", nuevo_numero in r.text)
check("no hay error de 'no hay ninguna propiedad con el número'", "no hay ninguna propiedad con el número" not in r.text)

m = re.search(r'name="datos" value=\'(.*?)\'>', r.text, re.S)
check("se encontro el campo datos", m is not None)
datos_json = m.group(1).replace("&#34;", '"').replace("&#39;", "'")
r = s.post(f"{BASE}/propiedades/importar/confirmar", data={"datos": datos_json}, allow_redirects=True)
check("confirmar importacion", r.status_code == 200)
check("el numero se actualizo de verdad", nuevo_numero in r.text)

# Ahora, SIN volver a descargar la plantilla (para simular que el admin ya
# había descargado el archivo antes de que el número cambiara), volvemos a
# subir el mismo archivo (ya con ZZZ-9 en el Número) — debe seguir
# funcionando porque empareja por ID, no por número.
buf.seek(0)
r2 = s.post(f"{BASE}/propiedades/importar", files={"archivo": ("plantilla.xlsx", buf, "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")})
check("segunda subida (mismo ID, numero ya cambiado) no truena", r2.status_code == 200 and "Así van a quedar" in r2.text)
check("segunda subida no marca error de ID inexistente", "no hay ninguna propiedad con ID" not in r2.text)

# ---------------------------------------------------------------------------
# 2. Colores de saldo: verificar que la clase 'saldo-cero' ya no es verde
# ---------------------------------------------------------------------------
with open("app/static/css/style.css", encoding="utf-8") as f:
    css = f.read()
check("saldo-cero ya no usa el verde de saldo-favor", ".saldo-cero { color: var(--gris-900); }" in css)
check("saldo-debe sigue rojo", ".saldo-debe { color: #b3261e; }" in css)
check("saldo-favor sigue verde", ".saldo-favor { color: #0a7d3c; }" in css)

r = s.get(f"{BASE}/cartera")
check("cartera responde", r.status_code == 200)
r = s.get(f"{BASE}/reporte")
check("reporte responde", r.status_code == 200)

# ---------------------------------------------------------------------------
# 3. Menú superior fijo: header + nav envueltos en .barra-superior sticky
# ---------------------------------------------------------------------------
r = s.get(f"{BASE}/dashboard")
check("dashboard responde", r.status_code == 200)
check("header y nav están envueltos en barra-superior", '<div class="barra-superior">' in r.text)
check(".barra-superior es sticky en el css", ".barra-superior {" in css and "position: sticky;" in css.split(".barra-superior {")[1].split("}")[0])

# ---------------------------------------------------------------------------
# 4. Cancelar un pago dentro de 48 horas
# ---------------------------------------------------------------------------
r = s.get(f"{BASE}/pagos/nuevo")
check("form de pago nuevo responde", r.status_code == 200)

propiedades_resp = s.get(f"{BASE}/cartera").text
m = re.search(r'value="(\d+)"[^>]*>\s*[\w-]', propiedades_resp)

# Tomamos una propiedad conocida por su saldo actual antes de pagar
import json as _json

hoy = dt.date.today().isoformat()
# Usamos propiedad_id=1 (existe siempre en el demo)
propiedad_id = 1

r = s.get(f"{BASE}/pagos/monto-sugerido/{propiedad_id}")
saldo_antes = r.json()["monto"]
print(f"   -> saldo sugerido de la propiedad {propiedad_id} antes de pagar: {saldo_antes}")

r = s.post(f"{BASE}/pagos/nuevo", data={
    "propiedad_id": str(propiedad_id), "fecha_recepcion": hoy, "monto": "500.00",
    "concepto": "mantenimiento", "proyecto_id": "", "metodo_pago": "efectivo",
    "correo_destino": "",
}, allow_redirects=True)
check("pago de prueba registrado", r.status_code == 200)
folio = re.search(r"AII-\d+-\d+", r.url).group(0)
print(f"   -> folio del pago de prueba: {folio}")

check("comprobante ofrece cancelar (dentro de 48h)", "Cancelar este pago" in r.text)

r = s.get(f"{BASE}/pagos/monto-sugerido/{propiedad_id}")
saldo_despues_de_pagar = r.json()["monto"]
check("el saldo bajó tras el pago", saldo_despues_de_pagar < saldo_antes or saldo_antes == 0)

# Cancelamos el pago
r = s.post(f"{BASE}/pagos/{'' }cancelar_placeholder", data={}) if False else None  # no-op, evita linter

# Necesitamos el id interno del pago, no solo el folio. Lo sacamos de /pagos.
r = s.get(f"{BASE}/pagos?folio={folio}")
m_id = re.search(r'/pagos/(\d+)/cancelar', r.text)
check("se encontró el botón de cancelar en /pagos", m_id is not None)
pago_id = m_id.group(1)

r = s.post(f"{BASE}/pagos/{pago_id}/cancelar", data={"volver": "pagos"}, allow_redirects=True)
check("cancelación redirige bien", r.status_code == 200)
check("mensaje de pago cancelado", "El pago se canceló" in r.text)
check("fila del pago se ve marcada como Cancelado", "Cancelado" in r.text)

r = s.get(f"{BASE}/pagos/monto-sugerido/{propiedad_id}")
saldo_tras_cancelar = r.json()["monto"]
check("el saldo vuelve a subir tras cancelar (ya no cuenta el pago)", abs(saldo_tras_cancelar - saldo_antes) < 0.01)

# Intentar cancelar de nuevo debe fallar con mensaje claro
r = s.post(f"{BASE}/pagos/{pago_id}/cancelar", data={"volver": "pagos"}, allow_redirects=True)
check("segunda cancelación no revienta", r.status_code == 200)
check("mensaje de ya estaba cancelado", "ya está cancelado" in r.text)

# El comprobante debe mostrar el estado cancelado
r = s.get(f"{BASE}/comprobantes/{folio}")
check("comprobante muestra pago cancelado", "Este pago está cancelado" in r.text)
check("comprobante ya no ofrece el botón de cancelar", "Cancelar este pago" not in r.text)

print("\nTODAS LAS PRUEBAS PASARON")
