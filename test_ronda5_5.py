"""Pruebas de la ronda 5.5 contra el servidor local (MODO_DEMO=1, base recién
borrada). Cubre: pagos con varias partidas, comprobante en PDF y envío por
correo, egresos con forma de pago y archivos, cartera sin «Esperado», reporte
en PDF (detalle y resumen) y destinatarios del correo."""
import datetime as dt
import io
import re

import requests

BASE = "http://127.0.0.1:8123"
s = requests.Session()


def check(nombre, cond):
    print(("OK  " if cond else "FAIL"), nombre)
    assert cond, nombre


r = s.post(f"{BASE}/login", data={"login_email": "demo@aii.mx", "password": "demo1234"})
check("login demo", r.status_code == 200)

# --- Estilos versionados y logo ------------------------------------------------
r = s.get(f"{BASE}/dashboard")
check("hoja de estilos lleva ?v=", re.search(r'/static/css/style\.css\?v=[0-9a-f]{10}', r.text) is not None)
check("encabezado con el nombre del conjunto (ronda 5.7)", "Auto-administración de " in r.text)
check("logo existe", s.get(f"{BASE}/static/img/logo.png").status_code == 200)
check("logo con nombre existe", s.get(f"{BASE}/static/img/logo-con-nombre.png").status_code == 200)
check("favicon existe", s.get(f"{BASE}/static/img/favicon.png").status_code == 200)
css = s.get(f"{BASE}/static/css/style.css").text
check("ancho de escritorio 1200px", "max-width: 1200px" in css)
check("enlace-boton sin fondo al pasar el mouse", ".enlace-boton:hover { background: none;" in css)

# --- Cartera sin «Esperado» ---------------------------------------------------
r = s.get(f"{BASE}/cartera")
check("cartera ya no tiene columna Esperado", "<th>Esperado</th>" not in r.text)

# --- Pago con varias partidas ---------------------------------------------------
r = s.get(f"{BASE}/pagos/nuevo")
# Una propiedad que deba al menos $1,300, para que la prueba del saldo diga algo.
candidatas = re.findall(r'<option value="(\d+)"', r.text.split('id="propiedad_id"')[1].split("</select>")[0])
propiedad_id = next(
    pid for pid in candidatas
    if s.get(f"{BASE}/pagos/monto-sugerido/{pid}").json()["monto"] >= 1300
)
proyecto_id = re.search(r'name="proyecto_id" class="sel-proyecto">\s*<option value="(\d+)"', r.text).group(1)

sug = s.get(f"{BASE}/pagos/monto-sugerido/{propiedad_id}").json()
check("monto sugerido trae desglose", {"monto", "mantenimiento", "proyectos"} <= set(sug))

saldo_antes = s.get(f"{BASE}/pagos/monto-sugerido/{propiedad_id}").json()["monto"]
hoy = dt.date.today().isoformat()
r = s.post(f"{BASE}/pagos/nuevo", data={
    "propiedad_id": propiedad_id, "fecha_recepcion": hoy, "metodo_pago": "deposito",
    "concepto": ["mantenimiento", "gas", "proyecto"],
    "monto": ["1000", "450", "300"],
    "proyecto_id": ["", "", proyecto_id],
})
check("pago con 3 partidas registrado", r.status_code == 200 and "/comprobantes/" in r.url)
folio = r.url.rsplit("/comprobantes/", 1)[1].split("?")[0]
check("comprobante muestra total del depósito", "$1,750.00" in r.text or "1,750.00" in s.get(f"{BASE}/pagos?folio={folio}").text)

saldo_despues = s.get(f"{BASE}/pagos/monto-sugerido/{propiedad_id}").json()["monto"]
# Mantenimiento (1000) y proyecto (300) bajan la deuda; el gas (450) no.
esperado = max(round(saldo_antes - 1300, 2), 0.0)
check(f"solo mantenimiento+proyecto bajan la deuda ({saldo_antes} -> {saldo_despues})",
      abs(saldo_despues - esperado) < 0.01)

r = s.get(f"{BASE}/pagos?folio={folio}")
check("la lista muestra UN renglón para el recibo", r.text.count(f">\n        {folio}\n") + r.text.count(f"{folio}\n        <") >= 1)
check("la lista desglosa las partidas", "Gas: $450.00" in r.text and "Mantenimiento: $1,000.00" in r.text)

img = s.get(f"{BASE}/comprobantes/{folio}/imagen")
check("imagen del comprobante", img.status_code == 200 and img.headers["content-type"] == "image/png")
pdf = s.get(f"{BASE}/comprobantes/{folio}/pdf")
check("PDF del comprobante", pdf.status_code == 200 and pdf.content[:4] == b"%PDF")
check("partida -2 lleva al mismo recibo", s.get(f"{BASE}/comprobantes/{folio}-2/imagen").content == img.content)

r = s.post(f"{BASE}/comprobantes/{folio}/enviar", data={"correo": "vecino@ejemplo.com"})
check("enviar comprobante por correo", r.status_code == 200 and "vecino@ejemplo.com" in r.text)
r = s.post(f"{BASE}/comprobantes/{folio}/enviar", data={"correo": "no-es-correo"})
check("correo inválido se rechaza", "no parece válido" in r.text)

r = s.get(f"{BASE}/pagos?concepto=gas&folio={folio}")
check("filtro por Gas encuentra el depósito combinado", folio in r.text)

# Cancelar cancela todas las partidas
r = s.get(f"{BASE}/pagos?folio={folio}")
pago_id = re.search(r'action="/pagos/(\d+)/cancelar"', r.text).group(1)
r = s.post(f"{BASE}/pagos/{pago_id}/cancelar", data={"volver": "pagos"})
saldo_cancelado = s.get(f"{BASE}/pagos/monto-sugerido/{propiedad_id}").json()["monto"]
check("cancelar el recibo devuelve la deuda completa", abs(saldo_cancelado - saldo_antes) < 0.01)

# Validaciones del formulario
r = s.post(f"{BASE}/pagos/nuevo", data={
    "propiedad_id": propiedad_id, "fecha_recepcion": hoy, "metodo_pago": "efectivo",
    "concepto": ["mantenimiento"], "monto": [""], "proyecto_id": [""],
})
check("sin monto no registra", "al menos un concepto" in r.text)

# El demo trae recibos combinados (mantenimiento + proyecto)
r = s.get(f"{BASE}/pagos?todos=1")
check("el demo tiene depósitos combinados", "lista-partidas" in r.text)

# --- Egresos --------------------------------------------------------------------
png = io.BytesIO()
from PIL import Image  # noqa: E402

Image.new("RGB", (40, 40), "white").save(png, format="PNG")
s.post(f"{BASE}/configuracion/modo-interfaz", data={"modo": "completo"})
r = s.post(f"{BASE}/egresos/nuevo", data={"concepto": "Prueba sin recibo", "monto": "100", "fecha": hoy, "forma_pago": "efectivo"})
check("egreso sin recibo se rechaza", "Falta subir el recibo" in r.text)
r = s.post(f"{BASE}/egresos/nuevo", data={"concepto": "Prueba sin forma", "monto": "100", "fecha": hoy},
           files={"recibo": ("r.png", png.getvalue(), "image/png")})
check("egreso sin forma de pago se rechaza", "forma en que se hizo el pago" in r.text)
r = s.post(f"{BASE}/egresos/nuevo", data={"concepto": "Prueba XML malo", "monto": "100", "fecha": hoy, "forma_pago": "cheque"},
           files={"recibo": ("r.png", png.getvalue(), "image/png"), "xml": ("f.pdf", b"%PDF-1.4", "application/pdf")})
check("XML con otra extensión se rechaza", "solo se acepta un archivo .xml" in r.text)
r = s.post(f"{BASE}/egresos/nuevo", data={"concepto": "Pintura portón", "monto": "2500", "fecha": hoy, "forma_pago": "transferencia"},
           files={
               "recibo": ("recibo.png", png.getvalue(), "image/png"),
               "xml": ("factura.xml", b"<cfdi/>", "text/xml"),
               "comprobante": ("transferencia.pdf", b"%PDF-1.4 prueba", "application/pdf"),
           })
check("egreso completo registrado", r.status_code == 200 and "Pintura portón" in r.text)
egreso_id = re.search(r'/egresos/(\d+)/archivo/recibo', r.text).group(1)
check("forma de pago visible", "Transferencia" in r.text)
check("recibo se descarga", s.get(f"{BASE}/egresos/{egreso_id}/archivo/recibo").content == png.getvalue())
check("xml se descarga", s.get(f"{BASE}/egresos/{egreso_id}/archivo/xml").content == b"<cfdi/>")
check("comprobante se descarga", s.get(f"{BASE}/egresos/{egreso_id}/archivo/comprobante").status_code == 200)
anon = requests.get(f"{BASE}/egresos/{egreso_id}/archivo/recibo", allow_redirects=False)
check("sin sesión no se ve el archivo", anon.status_code in (302, 303, 307) and "/login" in anon.headers.get("location", ""))

# --- Reporte --------------------------------------------------------------------
r = s.get(f"{BASE}/reporte")
check("reporte responde", r.status_code == 200)
anio, mes = re.search(r'/reporte/pdf\?anio=(\d+)&amp;mes=(\d+)', r.text).groups()
check("reporte en detalle muestra Por propiedad", "Por propiedad" in r.text)
r = s.get(f"{BASE}/reporte?anio={anio}&mes={mes}&version=resumen")
check("resumen: monto por cobrar y pagado por adelantado",
      "Monto por cobrar" in r.text and "Monto pagado por adelantado" in r.text and "<h2>Por propiedad</h2>" not in r.text)

for version in ("detalle", "resumen"):
    pdf = s.get(f"{BASE}/reporte/pdf?anio={anio}&mes={mes}&version={version}")
    check(f"PDF del reporte ({version})", pdf.status_code == 200 and pdf.content[:4] == b"%PDF" and len(pdf.content) > 5000)
    with open(f"/tmp/reporte_{version}.pdf", "wb") as f:
        f.write(pdf.content)

r = s.post(f"{BASE}/reporte/enviar?anio={anio}&mes={mes}", data={"modo": "duenos", "extras": "comite@ejemplo.com", "version": "resumen"})
check("envío a todos los dueños", "correos se guardaron como vista previa" in r.text or "Se envió a" in r.text)
check("recuerda la elección y los extras", 'value="comite@ejemplo.com"' in s.get(f"{BASE}/reporte").text)

r = s.post(f"{BASE}/reporte/enviar?anio={anio}&mes={mes}", data={"modo": "seleccion", "extras": ""})
check("selección vacía avisa", "No hay ningún correo" in r.text)

print("\nTODAS LAS PRUEBAS DE LA RONDA 5.5 PASARON")
