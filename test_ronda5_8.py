"""Pruebas de la ronda 5.8 (usabilidad y alcance del plan básico).

Configuración agrupada en secciones, egresos y proyectos en página propia,
proveedor del proyecto, reportes a 3 meses y sin nombre del propietario,
comprobante automático a los correos de la cuenta, plan básico sin archivos
en egresos / sin cotizaciones / sin recordatorios, aviso de traspaso, pie de
página y cierre de sesión por inactividad.

Levantar antes:  MODO_DEMO=1 uvicorn app.main:app --host 127.0.0.1 --port 8123
con data/aii.db recién borrada.
"""
import datetime as dt
import glob
import io
import os
import re
import sqlite3
import time
import zipfile

import requests

BASE = "http://127.0.0.1:8123"
RAIZ = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(RAIZ, "data", "aii.db")
CORREOS = os.path.join(RAIZ, "data", "correos_enviados")


def check(nombre, cond):
    print(("OK  " if cond else "FAIL"), nombre)
    assert cond, nombre


def sql(q, args=()):
    con = sqlite3.connect(DB)
    try:
        cur = con.execute(q, args)
        con.commit()
        return cur.fetchall()
    finally:
        con.close()


def correos_desde(t0):
    return [open(f, encoding="utf-8").read()
            for f in sorted(glob.glob(os.path.join(CORREOS, "*.html")))
            if os.path.getmtime(f) >= t0]


s = requests.Session()
s.post(f"{BASE}/registro", data={
    "nombre_conjunto": "Bosque Verde T2", "direccion": "", "admin_nombre": "SBM",
    "login_email": "admin58@ejemplo.mx", "password": "clave1234", "cuenta_email": "cuenta58@ejemplo.mx",
    "monto_mensual": "1000", "monto_revision_meses": "12", "fecha_limite_pago": "11",
    "saldo_inicial": "0", "num_propiedades": "5"})
cid = sql("select id from conjuntos where nombre='Bosque Verde T2'")[0][0]
s.post(f"{BASE}/configuracion/modo-interfaz", data={"modo": "completo"})
props = [p[0] for p in sql("select id from propiedades where conjunto_id=? order by id", (cid,))]
for pid, n in zip(props[:3], ["101", "102", "103"]):
    s.post(f"{BASE}/propiedades/{pid}/actualizar", data={
        "numero": n, "tipo": "departamento", "nombre_dueno": f"Dueño {n}",
        "email_dueno": f"dueno{n}@ejemplo.mx", "tipo_saldo": "cero", "monto_saldo": "0"})
hoy = dt.date.today().isoformat()

# --- Configuración agrupada -------------------------------------------------
r = s.get(f"{BASE}/configuracion")
for seccion in ["Modo de interfaz", "Datos del conjunto", "Propiedades", "Tu cuenta", "Traspaso de administración"]:
    check(f"la portada de Configuración muestra «{seccion}»", seccion in r.text)
check("la portada ya no trae los formularios", "name=\"nuevo_monto\"" not in r.text and "name=\"password_actual\"" not in r.text)
for ruta, debe, no_debe in [
    ("/configuracion/interfaz", 'name="modo"', "nuevo_monto"),
    ("/configuracion/conjunto", 'name="nuevo_monto"', 'name="password_nueva"'),
    ("/configuracion/propiedades", 'name="nombre_dueno"', 'name="nuevo_monto"'),
    ("/configuracion/cuenta", 'name="password_nueva"', 'name="nuevo_monto"'),
]:
    r = s.get(BASE + ruta)
    check(f"{ruta} abre con lo suyo", r.status_code == 200 and debe in r.text)
    check(f"{ruta} no arrastra otras secciones", no_debe not in r.text)
    check(f"{ruta} tiene botón para regresar", 'href="/configuracion"' in r.text)
r = s.get(f"{BASE}/administrador")
check("el traspaso tiene botón para regresar", 'href="/configuracion"' in r.text)

# Propiedades: lectura primero, edición bajo botón
r = s.get(f"{BASE}/configuracion/propiedades")
check("la ficha se ve en modo lectura", 'class="ficha-lectura"' in r.text and "Dueño 101" in r.text)
check("el formulario de edición está oculto hasta dar clic", re.search(r'id="editar-\d+" hidden', r.text) is not None)
check("hay botón Editar por propiedad", 'onclick="editarFicha(' in r.text)
check("agregar propiedad está oculto hasta pedirlo", 'id="bloque-agregar" hidden' in r.text and "Agregar propiedad" in r.text)

# Nombre del conjunto editable, y los formularios regresan a su sección
r = s.post(f"{BASE}/configuracion/direccion", data={"nombre": "Bosque Verde Torre 2", "direccion": "Av. Siempre Viva 1"})
check("el nombre del conjunto se puede cambiar", r.url.endswith("/configuracion/conjunto")
      and sql("select nombre from conjuntos where id=?", (cid,))[0][0] == "Bosque Verde Torre 2")
r = s.post(f"{BASE}/configuracion/fecha-limite", data={"fecha_limite_pago": "10"})
check("guardar la fecha límite regresa a Datos del conjunto", r.url.endswith("/configuracion/conjunto"))
r = s.post(f"{BASE}/configuracion/cuenta-email", data={"cuenta_email": "cuenta58b@ejemplo.mx"})
check("el correo de la cuenta se edita desde Tu cuenta", "Correo de la cuenta actualizado" in r.text
      and sql("select cuenta_email from conjuntos where id=?", (cid,))[0][0] == "cuenta58b@ejemplo.mx")
r = s.post(f"{BASE}/configuracion/cuenta-email", data={"cuenta_email": "esto-no-es-correo"})
check("un correo inválido se rechaza", "no parece válido" in r.text)

# --- Egresos: página propia, sin archivos en el plan básico -----------------
r = s.get(f"{BASE}/egresos")
check("la lista de egresos ya no trae el formulario", 'action="/egresos/nuevo"' not in r.text
      and 'href="/egresos/nuevo"' in r.text)
r = s.get(f"{BASE}/egresos/nuevo")
check("el alta de egreso es su propia página", r.status_code == 200 and 'action="/egresos/nuevo"' in r.text)
check("el alta de egreso no muestra la lista de egresos", "<table>" not in r.text)
check("plan básico: el egreso no pide archivos", 'type="file"' not in r.text)
r = s.post(f"{BASE}/egresos/nuevo", data={"concepto": "Jardinería", "monto": "1500", "fecha": hoy, "forma_pago": "efectivo"})
check("se registra un egreso sin recibo", r.url.endswith("/egresos")
      and sql("select count(*) from egresos where conjunto_id=? and concepto='Jardinería'", (cid,))[0][0] == 1)
r = s.post(f"{BASE}/egresos/nuevo", data={"concepto": "Sin forma", "monto": "10", "fecha": hoy, "forma_pago": ""})
check("sin forma de pago se rechaza y regresa a la página del alta", "Elige la forma en que se hizo el pago" in r.text)

# --- Proyectos: página propia, proveedor, sin cotizaciones ------------------
r = s.get(f"{BASE}/proyectos")
check("la lista de proyectos ya no trae el formulario", 'action="/proyectos/nuevo"' not in r.text
      and 'href="/proyectos/nuevo"' in r.text)
r = s.get(f"{BASE}/proyectos/nuevo")
check("el alta de proyecto es su propia página", r.status_code == 200 and 'action="/proyectos/nuevo"' in r.text)
check("plan básico: sin cotizaciones", "Cotización 1" not in r.text and "Cotizaciones" not in r.text)
check("el alta pide proveedor obligatorio", 'name="proveedor_nombre"' in r.text and "required" in r.text)
r = s.post(f"{BASE}/proyectos/nuevo", data={"concepto": "Impermeabilización", "monto_total": "30000",
          "estado": "en_recaudacion", "financiamiento": "mixto", "financiamiento_pct_fondo": "50",
          "proveedor_nombre": "Impermeables del Centro", "proveedor_email": "ventas@imper.mx",
          "proveedor_telefono": "+52 55 1234 5678"})
fila = sql("select id, proveedor_nombre, proveedor_email, proveedor_telefono, monto_por_propiedad from proyectos where conjunto_id=?", (cid,))
check("el proveedor se guarda con el proyecto", fila and fila[0][1] == "Impermeables del Centro"
      and fila[0][2] == "ventas@imper.mx" and fila[0][3] == "+52 55 1234 5678")
check("el reparto mixto sigue bien (50% de $30,000 entre 5 = $3,000)", fila[0][4] == 3000)
pid_proy = fila[0][0]
check("el proveedor se ve en la lista", "Impermeables del Centro" in s.get(f"{BASE}/proyectos").text)
r = s.post(f"{BASE}/proyectos/nuevo", data={"concepto": "Sin proveedor", "monto_total": "100",
          "estado": "por_iniciar", "financiamiento": "fondo", "proveedor_nombre": ""})
check("sin proveedor se rechaza", "Falta el nombre del proveedor" in requests.utils.unquote(r.url)
      and sql("select count(*) from proyectos where concepto='Sin proveedor'")[0][0] == 0)
r = s.get(f"{BASE}/proyectos/{pid_proy}/editar")
check("la edición también trae el proveedor", 'value="Impermeables del Centro"' in r.text)
check("plan básico: la edición no muestra cotizaciones", "Cotización 1" not in r.text)

# --- Egreso ligado a proyecto ------------------------------------------------
r = s.get(f"{BASE}/egresos/nuevo")
check("el alta de egreso despliega los proyectos activos", "Impermeabilización" in r.text
      and "function toggleProyectoEgreso" in r.text)

# --- Pago: comprobante automático a los correos de la cuenta ----------------
r = s.get(f"{BASE}/pagos/nuevo")
check("plan básico: ya no se pide el correo del vecino", 'name="correo_destino"' not in r.text)
check("se avisa a dónde se manda el comprobante", "cuenta58b@ejemplo.mx" in r.text and "admin58@ejemplo.mx" in r.text)
t0 = time.time() - 1
r = s.post(f"{BASE}/pagos/nuevo", data={"propiedad_id": str(props[0]), "fecha_recepcion": hoy,
           "metodo_pago": "efectivo", "concepto": "mantenimiento", "proyecto_id": "",
           "monto": "1000", "concepto_descripcion": ""})
check("el pago se registra", "/comprobantes/" in r.url)
enviados = [c for c in correos_desde(t0) if "Comprobante de pago" in c or "comprobante" in c.lower()]
texto = " ".join(enviados)
check("el comprobante se manda solo al correo de la cuenta y al principal",
      "cuenta58b@ejemplo.mx" in texto and "admin58@ejemplo.mx" in texto)

# --- Reportes ----------------------------------------------------------------
d = requests.Session()
d.post(f"{BASE}/login", data={"login_email": "demo@aii.mx", "password": "demo1234"})
r = d.get(f"{BASE}/reporte")
check("plan básico: 3 meses de reporte", len(re.findall(r'<option value="\d{4}-\d{1,2}"', r.text)) == 3)
check("el reporte detallado ya no trae el nombre del propietario",
      "Administra " in r.text and not re.search(r'<td>\s*\d+\s*<div class="ayuda">[A-ZÁÉÍÓÚÑ]', r.text))
pdf = d.get(f"{BASE}/reporte/pdf?detalle=completo").content
check("el PDF del reporte se genera", pdf[:4] == b"%PDF" and len(pdf) > 5000)

# --- Historial ----------------------------------------------------------------
z = zipfile.ZipFile(io.BytesIO(d.get(f"{BASE}/configuracion/descargar").content))
check("el historial sigue trayendo Excel y comprobantes", "historial.xlsx" in z.namelist()
      and any(n.startswith("comprobantes/") for n in z.namelist()))

# --- Traspaso de administración ----------------------------------------------
t1 = time.time() - 1
r = s.post(f"{BASE}/administrador/actualizar", data={"password_actual": "clave1234", "admin_nombre_nuevo": "Laura M.",
           "login_email_nuevo": "Laura@Ejemplo.mx", "password_nueva": "nueva12345",
           "correo_recuperacion_nuevo": ""})
check("el traspaso se aplica y cierra la sesión", "/login?traspaso=1" in r.url)
avisos = correos_desde(t1)
texto = " ".join(a for a in avisos if "Cambio de administrador" in a)
check("se avisa del traspaso a los propietarios con correo", "dueno101@ejemplo.mx" in texto and "dueno103@ejemplo.mx" in texto)
check("el aviso nombra al saliente y al entrante", "SBM" in texto and "Laura M." in texto)
check("el correo de login queda en minúsculas", sql("select login_email from conjuntos where id=?", (cid,))[0][0] == "laura@ejemplo.mx")
s2 = requests.Session()
r = s2.post(f"{BASE}/login", data={"login_email": "LAURA@ejemplo.mx", "password": "nueva12345"})
check("el nuevo administrador entra (sin distinguir mayúsculas)", r.url.endswith("/dashboard"))

# --- Pie de página y sesión ---------------------------------------------------
r = s2.get(f"{BASE}/dashboard")
check("pie: Auto-administración de conjuntos", "Auto-administración de conjuntos" in r.text)
check("pie: Administración Inteligente de Inmuebles", 'class="pie-marca">Administración Inteligente de Inmuebles' in r.text)
check("pie: by Omnera.mx", "by Omnera.mx" in r.text)
check("pie: lleva el mismo icono del encabezado", 'class="pie-logo"' in r.text)
check("ya no dice MVP en el pie", "MVP — Administración" not in r.text)

cookie = [c for c in s2.cookies if c.name == "session"][0]
restante = cookie.expires - time.time() if cookie.expires else 0
check("la sesión caduca a las 8 horas de inactividad", 7.5 * 3600 < restante <= 8 * 3600 + 60)
r = requests.Session().get(f"{BASE}/cartera")
check("sin sesión no se entra a la cuenta de nadie", r.url.endswith("/login"))

print("\nTODAS LAS PRUEBAS DE LA RONDA 5.8 PASARON")
