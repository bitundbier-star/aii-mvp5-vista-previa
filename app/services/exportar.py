"""Descarga de todo el historial del conjunto, en un solo archivo ZIP.

Existe por una razón concreta: antes de borrar una cuenta hay que poder
llevarse lo que había dentro. El administrador en turno que aprieta "borrar" no
está borrando lo suyo — está borrando las cuentas de todos sus vecinos, incluidos
los comprobantes que ya les entregó. Ofrecerle la descarga convierte una
decisión abstracta en una concreta, y le deja algo en la mano si se arrepiente.

El ZIP trae dos cosas, porque sirven para cosas distintas:

* **Archivos CSV** — se abren en Excel. Son los datos crudos, para seguir
  trabajando con ellos o cargarlos en otro lado. Llevan marca de orden de bytes
  para que Excel no destroce los acentos.
* **historial.html** — se abre de doble clic y se lee. Es la versión para
  imprimir, guardar o mandarle a la asamblea.

No agrega ninguna dependencia: `zipfile` y `csv` vienen con Python.
"""
import csv
import datetime as dt
import io
import zipfile

from .cartera import estado_conjunto, orden_natural
from .formato import dinero, estado_saldo, mes_titulo
from .reportes import datos_reporte, meses_disponibles, saldo_acumulado


def _csv(encabezados, filas) -> bytes:
    buffer = io.StringIO()
    escritor = csv.writer(buffer)
    escritor.writerow(encabezados)
    escritor.writerows(filas)
    return buffer.getvalue().encode("utf-8-sig")


def _propiedades(conjunto):
    filas = []
    for p in sorted(conjunto.propiedades, key=orden_natural):
        est = estado_saldo(p.saldo_inicial)
        filas.append([
            p.numero, p.tipo_legible, p.nombre_dueno or "",
            p.celular_dueno or "", p.email_dueno or "",
            p.nombre_residente or "", p.celular_residente or "",
            p.email_residente or "", p.notas or "",
            f"{abs(p.saldo_inicial):.2f}", est["palabra"],
            "Sí" if p.activo else "No",
        ])
    return _csv([
        "Número", "Tipo", "Propietario", "Celular propietario", "Correo propietario",
        "Residente", "Celular residente", "Correo residente", "Notas",
        "Saldo inicial", "Situación del saldo inicial", "Activa",
    ], filas)


def _pagos(conjunto):
    filas = []
    for p in sorted(conjunto.pagos, key=lambda x: (x.fecha_recepcion, x.id)):
        filas.append([
            p.folio, p.fecha_recepcion.isoformat(), p.propiedad.numero,
            p.propiedad.nombre_dueno or "", p.concepto_legible,
            p.proyecto.concepto if p.proyecto else "",
            p.metodo_pago_legible, f"{p.monto:.2f}",
            "Sí" if p.abona_a_cartera else "No",
        ])
    return _csv([
        "Folio", "Fecha", "Propiedad", "Propietario", "Concepto", "Proyecto",
        "Método de pago", "Monto", "Baja la deuda",
    ], filas)


def _egresos(conjunto):
    filas = [
        [e.fecha.isoformat(), e.concepto, f"{e.monto:.2f}"]
        for e in sorted(conjunto.egresos, key=lambda x: (x.fecha, x.id))
    ]
    return _csv(["Fecha", "Concepto", "Monto"], filas)


def _proyectos(conjunto):
    filas = []
    for p in sorted(conjunto.proyectos, key=lambda x: (x.fecha_alta, x.id)):
        filas.append([
            p.concepto, p.descripcion or "", p.fecha_alta.isoformat(),
            p.fecha_limite_pago.isoformat() if p.fecha_limite_pago else "",
            p.estado, p.comentario_estado or "",
            f"{p.monto_total:.2f}", f"{p.monto_por_propiedad:.2f}",
            f"{p.total_recaudado:.2f}",
        ])
    return _csv([
        "Proyecto", "Descripción", "Fecha de alta", "Fecha límite", "Estado",
        "Comentario", "Monto total", "Monto por propiedad", "Recaudado",
    ], filas)


def _cartera(conjunto):
    filas = []
    for e in estado_conjunto(conjunto):
        filas.append([
            e["propiedad"].numero, e["propiedad"].nombre_dueno or "",
            f"{e['total_esperado']:.2f}", f"{e['total_pagado']:.2f}",
            f"{abs(e['saldo']):.2f}", e["vista"]["palabra"],
        ])
    return _csv(
        ["Propiedad", "Propietario", "Esperado", "Pagado", "Saldo", "Situación"],
        filas,
    )


def _historial_html(conjunto) -> bytes:
    hoy = dt.date.today()
    estado = estado_conjunto(conjunto)
    meses = meses_disponibles(conjunto)

    filas_cartera = "".join(
        f"<tr><td>{e['propiedad'].numero}</td>"
        f"<td>{e['propiedad'].nombre_dueno or ''}</td>"
        f"<td class='num'>{e['total_esperado']|0 if False else dinero(e['total_esperado'])}</td>"
        f"<td class='num'>{dinero(e['total_pagado'])}</td>"
        f"<td class='num {e['vista']['clase']}'>{e['vista']['monto']}</td>"
        f"<td>{e['vista']['palabra']}</td></tr>"
        for e in estado
    )

    filas_meses = ""
    for m in meses:
        r = datos_reporte(conjunto, m["anio"], m["mes"])
        filas_meses += (
            f"<tr><td>{r['periodo']}</td>"
            f"<td class='num'>{dinero(r['saldo_apertura'])}</td>"
            f"<td class='num'>{dinero(r['ingresos_reales'])}</td>"
            f"<td class='num'>{dinero(r['total_egresos'])}</td>"
            f"<td class='num'><strong>{dinero(r['saldo_cierre'])}</strong></td></tr>"
        )

    html = f"""<!doctype html>
<html lang="es"><head><meta charset="utf-8">
<title>Historial de {conjunto.nombre}</title>
<style>
 body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif;
        color:#1a1a1a; max-width:900px; margin:0 auto; padding:36px 24px; line-height:1.6; }}
 h1 {{ font-size:26px; margin:0 0 4px; }}
 h2 {{ font-size:15px; text-transform:uppercase; letter-spacing:.05em; color:#666; margin:34px 0 10px; }}
 .sub {{ color:#666; margin:0 0 8px; }}
 table {{ width:100%; border-collapse:collapse; font-size:14px; }}
 th {{ text-align:left; color:#666; font-weight:600; border-bottom:1px solid #e5e5e5; padding:8px 6px; }}
 td {{ padding:7px 6px; border-bottom:1px solid #f0f0f0; }}
 .num {{ text-align:right; font-variant-numeric:tabular-nums; }}
 .saldo-debe {{ color:#b3261e; }} .saldo-favor, .saldo-cero {{ color:#0a7d3c; }}
 .nota {{ background:#eff6ff; border-left:3px solid #1d4ed8; padding:12px 16px;
          border-radius:0 8px 8px 0; font-size:14px; }}
 footer {{ margin-top:36px; padding-top:16px; border-top:1px solid #e5e5e5; color:#999; font-size:12px; }}
</style></head><body>

<h1>{conjunto.nombre}</h1>
{f'<p class="sub">{conjunto.direccion}</p>' if conjunto.direccion else ''}
<p class="sub">Historial completo · Administrador en turno: {conjunto.admin_nombre}</p>
<p class="sub">Generado el {hoy.strftime('%d/%m/%Y')}</p>

<div class="nota">
  Esta es la copia del historial del conjunto al momento de descargarla.
  Junto a este archivo, en el mismo ZIP, vienen los datos en formato CSV para
  abrirlos en Excel: propiedades, pagos, egresos, proyectos y cartera.
</div>

<h2>Resumen</h2>
<table>
  <tr><td>Propiedades registradas</td><td class="num">{len(conjunto.propiedades)}</td></tr>
  <tr><td>Pagos registrados</td><td class="num">{len(conjunto.pagos)}</td></tr>
  <tr><td>Egresos registrados</td><td class="num">{len(conjunto.egresos)}</td></tr>
  <tr><td>Proyectos</td><td class="num">{len(conjunto.proyectos)}</td></tr>
  <tr><td>Cuenta abierta desde</td><td class="num">{conjunto.fecha_inicio_cobros.strftime('%d/%m/%Y')}</td></tr>
  <tr><td><strong>Saldo acumulado del conjunto</strong></td>
      <td class="num"><strong>{dinero(saldo_acumulado(conjunto, hoy))}</strong></td></tr>
</table>

<h2>Cartera por propiedad</h2>
<table>
  <tr><th>Propiedad</th><th>Propietario</th><th class="num">Esperado</th>
      <th class="num">Pagado</th><th class="num">Saldo</th><th>Situación</th></tr>
  {filas_cartera}
</table>

<h2>Mes a mes</h2>
<table>
  <tr><th>Mes</th><th class="num">Saldo de apertura</th><th class="num">Ingresos</th>
      <th class="num">Egresos</th><th class="num">Saldo al cierre</th></tr>
  {filas_meses or '<tr><td colspan="5">Todavía no hay meses cerrados.</td></tr>'}
</table>

<footer>
  Administración Inteligente de Inmuebles · Herramienta para vecinos que se auto-administran.<br>
  Los saldos se muestran siempre en positivo; la palabra de la última columna dice si es adeudo o saldo a favor.
</footer>
</body></html>"""
    return html.encode("utf-8")


def exportar_conjunto(conjunto) -> tuple[bytes, str]:
    """Arma el ZIP con todo el historial. Devuelve (contenido, nombre)."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("historial.html", _historial_html(conjunto))
        z.writestr("propiedades.csv", _propiedades(conjunto))
        z.writestr("pagos.csv", _pagos(conjunto))
        z.writestr("egresos.csv", _egresos(conjunto))
        z.writestr("proyectos.csv", _proyectos(conjunto))
        z.writestr("cartera.csv", _cartera(conjunto))

    limpio = "".join(
        c if c.isalnum() or c in " -_" else "" for c in conjunto.nombre
    ).strip().replace(" ", "_") or "conjunto"
    nombre = f"{limpio}_historial_{dt.date.today().isoformat()}.zip"
    return buffer.getvalue(), nombre
