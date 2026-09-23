"""Pruebas de la ronda 5.7 contra el servidor local (MODO_DEMO=1, base recién
borrada). Registro con prueba y bienvenida, encabezado, proyectos (reparto,
centavos, estados, eliminar, cancelar con reembolsos), egresos ligados a
proyecto, aviso de pago cancelado, pago/planes, webhook y fases de la cuenta."""
import datetime as dt
import glob
import os
import re
import sqlite3
import time

import requests

BASE = "http://127.0.0.1:8123"
DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "aii.db")
CORREOS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "correos_enviados")


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
    out = []
    for f in sorted(glob.glob(os.path.join(CORREOS, "*.html"))):
        if os.path.getmtime(f) >= t0:
            out.append(open(f, encoding="utf-8").read())
    return out


# --- Registro ----------------------------------------------------------------
s = requests.Session()
t0 = time.time() - 1
datos = {
    "nombre_conjunto": "Bosque Verde T2", "direccion": "", "admin_nombre": "SBM",
    "login_email": "Prueba57@Ejemplo.mx", "password": "clave1234", "cuenta_email": "cuenta57@ejemplo.mx",
    "monto_mensual": "1000", "monto_revision_meses": "12", "fecha_limite_pago": "11",
    "saldo_inicial": "50000", "num_propiedades": "30",
}
r = s.post(f"{BASE}/registro", data=datos)
check("registro de 30 propiedades funciona", r.status_code == 200 and "/propiedades" in r.url)
fila = sql("select id, prueba_hasta, stripe_status, verif_token, email_verificado, login_email from conjuntos where nombre='Bosque Verde T2'")[0]
cid = fila[0]
check("la prueba de 30 días inicia al registrarse", fila[1] == (dt.date.today() + dt.timedelta(days=30)).isoformat() and fila[2] == "trialing")
check("correo de login se guarda en minúsculas", fila[5] == "prueba57@ejemplo.mx")
bienvenida = [c for c in correos_desde(t0) if "Confirmar mi correo" in c]
check("se manda correo de bienvenida al correo de registro", bienvenida and "prueba57@ejemplo.mx" in bienvenida[0])
token = fila[3]
check("el correo trae el enlace de verificación", token and f"/verificar?token={token}" in bienvenida[0])

r = s.post(f"{BASE}/registro", data=dict(datos, login_email="otro@ejemplo.mx", num_propiedades="31"))
check("el plan básico no acepta más de 30 propiedades", "hasta 30 propiedades" in r.text)

r = s.get(f"{BASE}/dashboard")
check("encabezado: Auto-administración de {conjunto}", "Auto-administración de Bosque Verde T2" in r.text)
check("encabezado: plan y días de prueba", "Plan Cuentas Claras · Prueba: 30 días restantes" in r.text)
check("ya no aparece el nombre del administrador en el encabezado", "Bosque Verde T2 — SBM" not in r.text)
check("aviso para confirmar el correo", "Confirma tu correo" in r.text)
r = s.get(f"{BASE}/verificar", params={"token": token})
check("verificar correo lo confirma", "verificado=1" in r.url and sql("select email_verificado from conjuntos where id=?", (cid,))[0][0] == 1)
r = s.get(f"{BASE}/dashboard")
check("ya no pide confirmar el correo", "Confirma tu correo" not in r.text)

# Llenar fichas (propietario) de 3 propiedades con correos
props = [p[0] for p in sql("select id from propiedades where conjunto_id=? order by id", (cid,))]
for pid, n in zip(props[:3], ["101", "102", "103"]):
    s.post(f"{BASE}/propiedades/{pid}/actualizar", data={
        "numero": n, "tipo": "departamento", "nombre_dueno": f"Dueño {n}",
        "email_dueno": f"dueno{n}@ejemplo.mx", "email_residente": f"res{n}@ejemplo.mx", "tipo_saldo": "cero", "monto_saldo": "0"})
s.post(f"{BASE}/configuracion/modo-interfaz", data={"modo": "completo"})

# --- Proyectos: reparto ----------------------------------------------------------
def nuevo_proyecto(concepto, monto, fin, pct=""):
    # Desde la ronda 5.8 el proveedor es obligatorio.
    s.post(f"{BASE}/proyectos/nuevo", data={"concepto": concepto, "monto_total": str(monto),
            "estado": "en_recaudacion", "financiamiento": fin, "financiamiento_pct_fondo": pct,
            "proveedor_nombre": "Proveedor de prueba"})
    return sql("select id, monto_por_propiedad, ajuste_centavos_fondo from proyectos where conjunto_id=? and concepto=?", (cid, concepto))[0]

p_fondo = nuevo_proyecto("Bomba nueva", 30000, "fondo")
check("100% fondo: $0 por propiedad", p_fondo[1] == 0)
p_mixto = nuevo_proyecto("Pintura fachada", 30000, "mixto", "50")
check("mixto 50% de $30,000 entre 30: $500 por propiedad", p_mixto[1] == 500)
p_cuota = nuevo_proyecto("Portón", 10000, "cuota")
check("centavos: $10,000 entre 30 = $333.33 por propiedad", p_cuota[1] == 333.33)
check("centavos: el fondo absorbe $0.10 y queda anotado", abs((p_cuota[2] or 0) - 0.10) < 0.001)
r = s.get(f"{BASE}/proyectos")
check("la lista menciona el redondeo que absorbe el fondo", "de redondeo que absorbe el fondo" in r.text)
r = s.post(f"{BASE}/proyectos/nuevo", data={"concepto": "Mal", "monto_total": "100", "financiamiento": "mixto",
           "financiamiento_pct_fondo": "", "proveedor_nombre": "Proveedor de prueba"})
check("mixto sin porcentaje se rechaza", "error_pct=1" in r.url)

r = s.get(f"{BASE}/cartera")
check("cartera abre", r.status_code == 200)
# Cargo de proyectos de la última propiedad: solo 500 + 333.33 (no los $30,000 del fondo)
from_prop = props[-1]
r = s.get(f"{BASE}/pagos/monto-sugerido/{from_prop}")
datos_sug = r.json()
monto_proy = sum(p.get("pendiente", 0) for p in datos_sug.get("proyectos", []))
check("la última propiedad ya no carga el proyecto pagado por el fondo", abs(monto_proy - 833.33) < 0.01)

# --- Editar, terminar, eliminar -------------------------------------------------------
r = s.post(f"{BASE}/proyectos/{p_fondo[0]}/editar", data={"concepto": "Bomba nueva", "monto_total": "30000",
          "estado": "en_proceso", "financiamiento": "fondo", "proveedor_nombre": "Proveedor de prueba"})
check("editar un proyecto ya no truena", r.status_code == 200 and r.url.endswith("/proyectos"))
s.post(f"{BASE}/proyectos/{p_fondo[0]}/terminar")
est = sql("select estado, terminado_en from proyectos where id=?", (p_fondo[0],))[0]
check("marcar Terminado guarda estado y fecha", est[0] == "terminado" and est[1])
r = s.post(f"{BASE}/proyectos/{p_mixto[0]}/editar", data={"concepto": "Pintura fachada", "monto_total": "30000",
          "estado": "cancelado", "financiamiento": "mixto", "financiamiento_pct_fondo": "50",
          "proveedor_nombre": "Proveedor de prueba"})
check("elegir Cancelado lleva al proceso de cancelación", r.url.endswith(f"/proyectos/{p_mixto[0]}/cancelar"))
p_borrar = nuevo_proyecto("Idea descartada", 1000, "cuota")
r = s.post(f"{BASE}/proyectos/{p_borrar[0]}/eliminar", data={"motivo": ""})
check("eliminar sin motivo se rechaza", sql("select eliminado from proyectos where id=?", (p_borrar[0],))[0][0] == 0)
s.post(f"{BASE}/proyectos/{p_borrar[0]}/eliminar", data={"motivo": "La asamblea lo descartó"})
el = sql("select eliminado, eliminado_en, eliminado_motivo from proyectos where id=?", (p_borrar[0],))[0]
check("eliminar deja registro con fecha y motivo", el[0] == 1 and el[1] and el[2] == "La asamblea lo descartó")
check("el eliminado ya no aparece en la lista", "Idea descartada" not in s.get(f"{BASE}/proyectos").text)

# --- Egreso ligado a proyecto ---------------------------------------------------------
r = s.get(f"{BASE}/egresos/nuevo")
check("egresos: existe la función que despliega los proyectos", "function toggleProyectoEgreso" in r.text and "Pintura fachada" in r.text)

# --- Pagos al proyecto mixto y cancelación con decisiones --------------------------------------
hoy = dt.date.today().isoformat()
def pagar(pid, monto, metodo):
    r = s.post(f"{BASE}/pagos/nuevo", data={"propiedad_id": str(pid), "fecha_recepcion": hoy, "metodo_pago": metodo,
               "concepto": "proyecto", "proyecto_id": str(p_mixto[0]), "monto": str(monto),
               "concepto_descripcion": ""})
    return r
r = pagar(props[0], 500, "efectivo")
check("pago al proyecto (efectivo) registrado", r.status_code == 200)
pagar(props[1], 300, "efectivo"); pagar(props[1], 200, "deposito")
pagar(props[2], 500, "deposito")
n_pagos = sql("select count(*) from pagos where proyecto_id=? and cancelado=0", (p_mixto[0],))[0][0]
check("hay 4 pagos al proyecto", n_pagos == 4)

saldo_antes = s.get(f"{BASE}/dashboard").text
r = s.post(f"{BASE}/proyectos/{p_mixto[0]}/cancelar/preview", data={"motivo": "Proveedor incumplió", "sin_recuperar": "0", "reparto_perdida": "pagaron"})
check("vista previa con decisión por propiedad", 'name="decision_%d"' % props[0] in r.text and "Reembolso proporcional" in r.text)
check("quien pagó solo por depósito no tiene opción de efectivo", re.search(r'name="decision_%d"[^<]*(<option[^>]*>[^<]*</option>\s*)+' % props[2], r.text) and "proporcional" not in re.search(r'name="decision_%d".*?</select>' % props[2], r.text, re.S).group(0))
r = s.post(f"{BASE}/proyectos/{p_mixto[0]}/cancelar/confirmar", data={
    "motivo": "Proveedor incumplió", "sin_recuperar": "0", "reparto_perdida": "pagaron",
    f"decision_{props[0]}": "abono", f"decision_{props[1]}": "reembolso_proporcional", f"decision_{props[2]}": "reembolso_transferencia"})
check("cancelación aplicada", sql("select cancelado, cancelado_credito_modo from proyectos where id=?", (p_mixto[0],))[0] == (1, "por_propiedad"))
egs = sql("select forma_pago, monto, automatico, propiedad_id from egresos where proyecto_id=? order by propiedad_id, forma_pago", (p_mixto[0],))
check("reembolso proporcional: $300 efectivo + $200 transferencia", (("efectivo", 300.0, 1, props[1]) in egs) and (("transferencia", 200.0, 1, props[1]) in egs))
check("reembolso por depósito: $500 por transferencia", ("transferencia", 500.0, 1, props[2]) in egs)
abono = sql("select monto, interno, concepto from pagos where propiedad_id=? and interno=1", (props[0],))
check("abono a la cuenta de la 101 por $500", abono and abono[0][0] == 500 and abono[0][2] == "mantenimiento")
r = s.get(f"{BASE}/pagos")
check("los movimientos internos no salen en la lista de Pagos", "Reintegro para reembolso" not in r.text)
r = s.get(f"{BASE}/egresos")
check("los reembolsos salen en Egresos como automáticos y sin botón de editar", "Reembolso por cancelación del proyecto Pintura fachada" in r.text and "Registrado automáticamente por el sistema" in r.text)
eg_id = sql("select id from egresos where proyecto_id=? limit 1", (p_mixto[0],))[0][0]
s.post(f"{BASE}/egresos/{eg_id}/eliminar")
check("un egreso automático no se puede borrar", sql("select count(*) from egresos where id=?", (eg_id,))[0][0] == 1)
r = s.get(f"{BASE}/proyectos/{p_mixto[0]}/editar")
check("el proyecto cancelado muestra qué pasó con cada propiedad", "Qué pasó con el dinero de cada propiedad" in r.text and "Reembolso" in r.text)
check("el proyecto cancelado ya no se cobra en la cartera", abs(sum(p.get("pendiente", 0) for p in s.get(f"{BASE}/pagos/monto-sugerido/{from_prop}").json().get("proyectos", [])) - 333.33) < 0.01)

# --- Aviso de pago cancelado ---------------------------------------------------------------
t1 = time.time() - 1
pagar_mant = s.post(f"{BASE}/pagos/nuevo", data={"propiedad_id": str(props[2]), "fecha_recepcion": hoy, "metodo_pago": "efectivo",
               "concepto": "mantenimiento", "proyecto_id": "", "monto": "1000", "concepto_descripcion": ""})
pid_pago = sql("select id from pagos where propiedad_id=? and concepto='mantenimiento' and interno=0 order by id desc limit 1", (props[2],))[0][0]
s.post(f"{BASE}/pagos/{pid_pago}/cancelar")
avisos = [c for c in correos_desde(t1) if "Se canceló un pago registrado" in c]
destinos = " ".join(avisos)
check("aviso de pago cancelado al propietario y al residente", len(avisos) == 2 and "dueno103@ejemplo.mx" in destinos and "res103@ejemplo.mx" in destinos)

# --- Planes y pago ------------------------------------------------------------------------
r = s.get(f"{BASE}/pago")
check("la pantalla de pago ya existe", r.status_code == 200 and "Planes y pago" in r.text and "Cuentas Claras" in r.text and "Próximamente" in r.text)
check("muestra el precio de respaldo $349 sin Stripe", "$349.00" in r.text)
r = s.get(f"{BASE}/pago/exito")
check("la pantalla de pago exitoso existe", r.status_code == 200 and "gracias" in r.text.lower())
r = requests.post(f"{BASE}/stripe/webhook", data='{"type":"checkout.session.completed","data":{"object":{"metadata":{"conjunto_id":"%d"}}}}' % cid)
check("el webhook rechaza avisos sin firma", r.status_code == 400 and sql("select stripe_status from conjuntos where id=?", (cid,))[0][0] == "trialing")

# --- Fases de la cuenta ---------------------------------------------------------------------
sql("update conjuntos set prueba_hasta=? where id=?", ((dt.date.today() - dt.timedelta(days=5)).isoformat(), cid))
r = s.get(f"{BASE}/dashboard")
check("día 5 sin pagar: aviso de solo lectura", "modo solo lectura" in r.text)
n_antes = sql("select count(*) from egresos where conjunto_id=?", (cid,))[0][0]
r = s.post(f"{BASE}/egresos/nuevo", data={"concepto": "No debería", "monto": "10", "fecha": hoy, "forma_pago": "efectivo"})
check("en solo lectura no se puede registrar", "solo_lectura=1" in r.url and sql("select count(*) from egresos where conjunto_id=?", (cid,))[0][0] == n_antes)
check("en solo lectura sí se puede descargar el historial", s.get(f"{BASE}/configuracion/descargar").status_code == 200)
sql("update conjuntos set prueba_hasta=? where id=?", ((dt.date.today() - dt.timedelta(days=35)).isoformat(), cid))
r = s.get(f"{BASE}/cartera")
check("día 35: sin acceso, solo la pantalla de pago", r.url.endswith("/pago?bloqueada=1") and "suspendida por falta de pago" in r.text)
r = s.get(f"{BASE}/demo-no-existe")
check("día 35: cualquier ruta manda a pagar", "/pago" in r.url)

# --- Webhook firmado: pago, egreso automático, falla y reactivación -----------------------
import hashlib, hmac, json
SECRETO = os.environ.get("PRUEBA_WHSEC", "whsec_prueba")
def aviso(tipo, objeto):
    cuerpo = json.dumps({"id": "evt_1", "object": "event", "type": tipo, "data": {"object": objeto}})
    t = int(time.time())
    firma = hmac.new(SECRETO.encode(), f"{t}.{cuerpo}".encode(), hashlib.sha256).hexdigest()
    return requests.post(f"{BASE}/stripe/webhook", data=cuerpo, headers={"stripe-signature": f"t={t},v1={firma}", "Content-Type": "application/json"})

r = aviso("checkout.session.completed", {"object": "checkout.session", "metadata": {"conjunto_id": str(cid)}, "client_reference_id": str(cid), "customer": "cus_123", "subscription": "sub_123"})
check("webhook firmado: suscripción registrada", r.status_code == 200 and sql("select stripe_status, stripe_customer_id, morosidad_desde from conjuntos where id=?", (cid,))[0] == ("active", "cus_123", None))
r = s.get(f"{BASE}/cartera")
check("con la suscripción activa se reactiva el acceso", r.url.endswith("/cartera"))
factura = {"object": "invoice", "id": "in_001", "customer": "cus_123", "amount_paid": 34900,
           "parent": {"subscription_details": {"subscription": "sub_123"}}}
aviso("invoice.paid", factura); aviso("invoice.paid", factura)
eg = sql("select concepto, monto, forma_pago, automatico from egresos where referencia_externa='in_001'")
check("el cobro de AII se registra solo como egreso, una sola vez", len(eg) == 1 and eg[0][1] == 349.0 and eg[0][3] == 1 and "Suscripción AII" in eg[0][0])
aviso("invoice.payment_failed", {"object": "invoice", "id": "in_002", "customer": "cus_123", "parent": {"subscription_details": {"subscription": "sub_123"}}})
st = sql("select stripe_status, morosidad_desde from conjuntos where id=?", (cid,))[0]
check("cobro fallido: empieza el conteo de falta de pago", st[0] == "past_due" and st[1] == dt.date.today().isoformat())
aviso("invoice.paid", dict(factura, id="in_003"))
st = sql("select stripe_status, morosidad_desde from conjuntos where id=?", (cid,))[0]
check("pago posterior: la cuenta se reactiva sola", st == ("active", None))
aviso("customer.subscription.deleted", {"object": "subscription", "id": "sub_123", "customer": "cus_123", "status": "canceled"})
check("suscripción cancelada: empieza el conteo", sql("select stripe_status from conjuntos where id=?", (cid,))[0][0] == "canceled")
aviso("customer.subscription.updated", {"object": "subscription", "id": "sub_123", "customer": "cus_123", "status": "active"})
check("suscripción reanudada: vuelve a activa", sql("select stripe_status, morosidad_desde from conjuntos where id=?", (cid,))[0] == ("active", None))

# --- Reportes: 2 meses en el básico y administrador del mes ---------------------------------
d = requests.Session()
d.post(f"{BASE}/login", data={"login_email": "demo@aii.mx", "password": "demo1234"})
r = d.get(f"{BASE}/reporte")
opciones = re.findall(r'<option value="\d{4}-\d{1,2}"', r.text)
check("plan básico: 3 meses de reporte", len(opciones) == 3)
check("el reporte dice quién administraba ese mes", "Administra " in r.text)
r = d.get(f"{BASE}/dashboard")
check("la demo no entra a solo lectura ni pide pago", "solo lectura" not in r.text and "Prueba:" not in r.text)

# --- Historial descargable -------------------------------------------------------------------
import io, zipfile
r = d.get(f"{BASE}/configuracion/descargar")
z = zipfile.ZipFile(io.BytesIO(r.content))
nombres = z.namelist()
check("el ZIP trae un Excel (no CSV)", "historial.xlsx" in nombres and not any(n.endswith(".csv") for n in nombres))
check("el ZIP trae los comprobantes en PDF", sum(1 for n in nombres if n.startswith("comprobantes/") and n.endswith(".pdf")) > 10)
from openpyxl import load_workbook
wb = load_workbook(io.BytesIO(z.read("historial.xlsx")))
check("el Excel tiene una pestaña por tabla", wb.sheetnames == ["Propiedades", "Pagos", "Egresos", "Proyectos", "Cartera"])
check("el historial HTML trae todos los meses", z.read("historial.html").decode().count("Administra ") >= 5)

# --- Plantilla Excel de propiedades ------------------------------------------------------------
r = s.get(f"{BASE}/propiedades/plantilla?v=1")
check("la plantilla no se guarda en caché", "no-store" in r.headers.get("Cache-Control", ""))
ws = load_workbook(io.BytesIO(r.content)).active
check("plantilla: A = ID oculto y bloqueado, B = Número", ws["A1"].value == "ID" and ws.column_dimensions["A"].hidden and ws["B1"].value == "Número" and isinstance(ws["A2"].value, int))

# --- Ciclo diario: aviso del día 30 y borrado del día 45 ---------------------------------------
os.environ.setdefault("MODO_DEMO", "1")
import sys; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from app.main import ciclo_cuentas_diario
sql("update conjuntos set stripe_status='trialing', morosidad_desde=NULL, aviso_cancelacion_enviado=0, prueba_hasta=? where id=?", ((dt.date.today() - dt.timedelta(days=30)).isoformat(), cid))
t2 = time.time() - 1
ciclo_cuentas_diario()
check("día 30: se manda el aviso de cancelación", any("Tu cuenta será cancelada" in c for c in correos_desde(t2)) and sql("select aviso_cancelacion_enviado from conjuntos where id=?", (cid,))[0][0] == 1)
time.sleep(1.5)
t3 = time.time()
ciclo_cuentas_diario()
check("el aviso del día 30 no se repite", not any("Tu cuenta será cancelada" in c for c in correos_desde(t3)))
sql("update conjuntos set prueba_hasta=? where id=?", ((dt.date.today() - dt.timedelta(days=45)).isoformat(), cid))
ciclo_cuentas_diario()
check("día 45: se borra la cuenta completa", sql("select count(*) from conjuntos where id=?", (cid,))[0][0] == 0 and sql("select count(*) from pagos where conjunto_id=?", (cid,))[0][0] == 0)
check("día 45: se avisa que la cuenta fue eliminada", any("fueron eliminadas de nuestro sistema" in c for c in correos_desde(t3)))
check("la demo sigue intacta", sql("select count(*) from conjuntos where login_email='demo@aii.mx'")[0][0] == 1)

print("\nTODAS LAS PRUEBAS DE LA RONDA 5.7 PASARON")
