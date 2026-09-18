"""El reporte mensual en PDF, generado en el servidor.

Hasta la ronda 5.4 el PDF salía de la impresión del navegador ("Guardar como
PDF"). Eso sirve para descargarlo a mano, pero no para adjuntarlo a un correo
ni para compartir el archivo por WhatsApp desde el celular: para eso el
servidor tiene que poder fabricar el PDF por su cuenta.

Se usa fpdf2: es Python puro (no necesita programas instalados en el
servidor) y no tiene ningún costo de operación.

Mismas reglas que la pantalla: ninguna cifra lleva signo negativo, y la
versión "resumen" no nombra a ninguna propiedad.
"""
import os

from fpdf import FPDF

from .formato import dinero

BASE_STATIC = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static")
FUENTE_REGULAR = os.path.join(BASE_STATIC, "fonts", "Poppins-Regular.ttf")
FUENTE_NEGRITA = os.path.join(BASE_STATIC, "fonts", "Poppins-Bold.ttf")
LOGO = os.path.join(BASE_STATIC, "img", "logo-con-nombre.png")

AZUL = (23, 107, 160)
VERDE = (10, 125, 60)
ROJO = (179, 38, 30)
TINTA = (26, 26, 26)
GRIS = (107, 114, 128)
LINEA = (226, 226, 230)
FONDO_VERDE = (242, 250, 245)


class _PDF(FPDF):
    def __init__(self, pie: str):
        super().__init__(orientation="P", unit="mm", format="Letter")
        self._pie = pie
        self.set_auto_page_break(auto=True, margin=18)
        self.set_margins(16, 16, 16)
        self.add_font("Poppins", "", FUENTE_REGULAR)
        self.add_font("Poppins", "B", FUENTE_NEGRITA)

    def footer(self):
        self.set_y(-13)
        self.set_font("Poppins", "", 7.5)
        self.set_text_color(*GRIS)
        self.cell(0, 5, f"{self._pie}  ·  Página {self.page_no()} de {{nb}}", align="C")


def _color_saldo(vista: dict):
    return {"debe": ROJO, "favor": VERDE}.get(vista["clave"], TINTA)


def generar_pdf_reporte(conjunto, reporte: dict, version: str = "detalle") -> bytes:
    generado = reporte["generado_en"]
    pie = (
        f"Del {reporte['primer_dia'].strftime('%d/%m/%Y')} al {reporte['ultimo_dia'].strftime('%d/%m/%Y')}  ·  "
        f"Generado el {generado.strftime('%d/%m/%Y a las %H:%M')}  ·  Administración Inteligente de Inmuebles"
    )
    pdf = _PDF(pie)
    pdf.add_page()
    ancho = pdf.w - pdf.l_margin - pdf.r_margin

    # --- Encabezado ------------------------------------------------------
    if os.path.exists(LOGO):
        pdf.image(LOGO, x=pdf.l_margin, y=pdf.t_margin, h=11)
        pdf.set_y(pdf.t_margin + 15)
    pdf.set_font("Poppins", "B", 17)
    pdf.set_text_color(*TINTA)
    pdf.cell(0, 9, f"Reporte mensual — {reporte['periodo']}", new_x="LMARGIN", new_y="NEXT")
    pdf.set_font("Poppins", "", 10)
    pdf.set_text_color(*GRIS)
    sub = f"{conjunto.nombre}  ·  Administra {conjunto.admin_nombre}"
    if getattr(conjunto, "direccion", ""):
        sub += f"  ·  {conjunto.direccion}"
    pdf.multi_cell(0, 5, sub, align="L", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    pdf.set_draw_color(*LINEA)
    pdf.line(pdf.l_margin, pdf.get_y(), pdf.w - pdf.r_margin, pdf.get_y())
    pdf.ln(5)

    # --- Los dos saldos --------------------------------------------------
    y = pdf.get_y()
    caja = (ancho - 6) / 2
    pdf.set_draw_color(*VERDE)
    pdf.set_fill_color(*FONDO_VERDE)
    pdf.set_line_width(0.6)
    pdf.rect(pdf.l_margin, y, caja, 24, style="DF", round_corners=True, corner_radius=3)
    pdf.set_draw_color(203, 213, 225)
    pdf.set_line_width(0.4)
    pdf.rect(pdf.l_margin + caja + 6, y, caja, 24, style="D", round_corners=True, corner_radius=3)
    pdf.set_line_width(0.2)

    pdf.set_xy(pdf.l_margin, y + 3)
    pdf.set_font("Poppins", "", 8.5)
    pdf.set_text_color(*GRIS)
    pdf.cell(caja, 5, "Saldo al cierre del mes", align="C")
    pdf.set_xy(pdf.l_margin, y + 9)
    pdf.set_font("Poppins", "B", 18)
    pdf.set_text_color(*VERDE)
    pdf.cell(caja, 11, dinero(reporte["saldo_cierre"]), align="C")

    pdf.set_xy(pdf.l_margin + caja + 6, y + 3)
    pdf.set_font("Poppins", "", 8.5)
    pdf.set_text_color(*GRIS)
    pdf.cell(caja, 5, "Saldo al inicio del mes", align="C")
    pdf.set_xy(pdf.l_margin + caja + 6, y + 10)
    pdf.set_font("Poppins", "B", 13)
    pdf.set_text_color(*TINTA)
    pdf.cell(caja, 9, dinero(reporte["saldo_apertura"]), align="C")
    pdf.set_y(y + 28)

    # --- Cifras del mes --------------------------------------------------
    total_props = reporte["propiedades_al_corriente"] + reporte["propiedades_con_adeudo"]
    cifras = [
        ("Ingresos reales del mes", dinero(reporte["ingresos_reales"]), TINTA),
        ("Egresos del mes", dinero(reporte["total_egresos"]), TINTA),
        ("Flujo de caja del mes", dinero(reporte["flujo_caja"]), VERDE if reporte["flujo_caja"] >= 0 else ROJO),
        ("Cartera por cobrar", dinero(reporte["cartera_total"]), ROJO if reporte["cartera_total"] > 0 else VERDE),
        ("Propiedades al corriente", f"{reporte['propiedades_al_corriente']} de {total_props}", TINTA),
    ]
    col = ancho / len(cifras)
    y = pdf.get_y()
    for i, (etiqueta, valor, color) in enumerate(cifras):
        x = pdf.l_margin + i * col
        pdf.set_xy(x, y)
        pdf.set_font("Poppins", "B", 11.5)
        pdf.set_text_color(*color)
        pdf.cell(col, 7, valor)
        pdf.set_xy(x, y + 7)
        pdf.set_font("Poppins", "", 7.5)
        pdf.set_text_color(*GRIS)
        pdf.multi_cell(col - 2, 3.8, etiqueta)
    if reporte["flujo_caja"] < 0:
        pdf.set_xy(pdf.l_margin, y + 15)
        pdf.set_font("Poppins", "", 7.5)
        pdf.cell(0, 4, "El flujo de caja fue negativo: salió más dinero del que entró.")
    pdf.set_y(y + 20)

    def titulo(texto):
        pdf.ln(3)
        if pdf.get_y() > pdf.h - 45:
            pdf.add_page()
        pdf.set_font("Poppins", "B", 11.5)
        pdf.set_text_color(*TINTA)
        pdf.cell(0, 8, texto, new_x="LMARGIN", new_y="NEXT")

    def nota(texto):
        pdf.set_font("Poppins", "", 7.8)
        pdf.set_text_color(*GRIS)
        pdf.multi_cell(0, 4, texto, align="L", new_x="LMARGIN", new_y="NEXT")

    def tabla(encabezados, anchos, filas, alinear, total=None):
        """filas: lista de listas de (texto, color|None)."""
        pdf.set_font("Poppins", "B", 7.8)
        pdf.set_text_color(*GRIS)
        pdf.set_draw_color(*LINEA)
        for enc, w, al in zip(encabezados, anchos, alinear):
            pdf.cell(w, 7, enc, border="B", align=al)
        pdf.ln()
        pdf.set_font("Poppins", "", 9)
        for fila in filas:
            if pdf.get_y() > pdf.h - 25:
                pdf.add_page()
            for (texto, color), w, al in zip(fila, anchos, alinear):
                pdf.set_text_color(*(color or TINTA))
                pdf.cell(w, 6.6, texto, border="B", align=al)
            pdf.ln()
        if total:
            pdf.set_font("Poppins", "B", 9)
            pdf.set_text_color(*TINTA)
            pdf.set_draw_color(*TINTA)
            y0 = pdf.get_y()
            pdf.line(pdf.l_margin, y0, pdf.l_margin + sum(anchos), y0)
            for texto, w, al in zip(total, anchos, alinear):
                pdf.cell(w, 7, texto, align=al)
            pdf.ln()
            pdf.set_draw_color(*LINEA)

    # --- Por propiedad (o resumen) ----------------------------------------
    if version == "resumen":
        titulo("Estado de la cartera")
        y = pdf.get_y()
        for i, (etiqueta, monto, color, detalle) in enumerate([
            ("Monto por cobrar", reporte["cartera_total"], ROJO,
             f"{reporte['propiedades_con_adeudo']} de {total_props} propiedades con adeudo"),
            ("Monto pagado por adelantado", reporte["a_favor_total"], VERDE,
             f"{reporte['propiedades_con_saldo_a_favor']} propiedades con saldo a favor"),
        ]):
            x = pdf.l_margin + i * (caja + 6)
            pdf.set_draw_color(203, 213, 225)
            pdf.rect(x, y, caja, 22, style="D", round_corners=True, corner_radius=3)
            pdf.set_xy(x, y + 2.5)
            pdf.set_font("Poppins", "", 8.5)
            pdf.set_text_color(*GRIS)
            pdf.cell(caja, 5, etiqueta, align="C")
            pdf.set_xy(x, y + 8)
            pdf.set_font("Poppins", "B", 14)
            pdf.set_text_color(*color)
            pdf.cell(caja, 7, dinero(monto), align="C")
            pdf.set_xy(x, y + 15.5)
            pdf.set_font("Poppins", "", 7.5)
            pdf.set_text_color(*GRIS)
            pdf.cell(caja, 4, detalle, align="C")
        pdf.set_y(y + 25)
        nota(f"Saldos al último día de {reporte['periodo']}. Esta versión del reporte no muestra el estado de cada propiedad.")
    else:
        titulo("Por propiedad")
        anchos = [ancho * 0.40, ancho * 0.22, ancho * 0.20, ancho * 0.18]
        filas = []
        for e in reporte["estado_propiedades"]:
            nombre = e["propiedad"].etiqueta
            if e["propiedad"].nombre_dueno:
                nombre += f" — {e['propiedad'].nombre_dueno}"
            color = _color_saldo(e["vista"])
            filas.append([
                (nombre[:48], None),
                (dinero(e["pagado_en_el_mes"]), None),
                (e["vista"]["monto"], color),
                (e["vista"]["palabra"], color),
            ])
        tabla(
            ["Propiedad", f"Pagado en {reporte['periodo']}", "Saldo acumulado", "Estatus"],
            anchos, filas, ["L", "R", "R", "R"],
            total=["Total", dinero(reporte["ingresos_reales"]), dinero(reporte["cartera_total"]), "por cobrar"],
        )
        nota(f"El saldo acumulado es al último día de {reporte['periodo']}. «Al corriente» incluye a quien no debe nada y a quien pagó por adelantado.")

    # --- Egresos -----------------------------------------------------------
    titulo(f"Egresos de {reporte['periodo']}")
    if reporte["egresos_mes"]:
        anchos = [ancho * 0.16, ancho * 0.50, ancho * 0.16, ancho * 0.18]
        filas = [
            [
                (e.fecha.strftime("%d/%m/%Y"), None),
                (e.concepto[:60], None),
                (getattr(e, "forma_pago_legible", "") or "—", GRIS),
                (dinero(e.monto), None),
            ]
            for e in reporte["egresos_mes"]
        ]
        tabla(["Fecha", "Concepto", "Forma de pago", "Monto"], anchos, filas, ["L", "L", "L", "R"],
              total=["Total de egresos del mes", "", "", dinero(reporte["total_egresos"])])
    else:
        nota(f"No se registró ningún egreso en {reporte['periodo']}.")

    # --- De dónde vino el dinero --------------------------------------------
    if reporte["ingresos_por_concepto"]:
        titulo("De dónde vino el dinero")
        anchos = [ancho * 0.7, ancho * 0.3]
        filas = [[(i["concepto"], None), (dinero(i["monto"]), None)] for i in reporte["ingresos_por_concepto"]]
        tabla(["Concepto", "Monto"], anchos, filas, ["L", "R"],
              total=["Total", dinero(reporte["ingresos_reales"])])
        nota("Solo Mantenimiento y Proyecto bajan la deuda de las propiedades. Gas, Agua y Otros entran al saldo del conjunto pero no reducen la cartera.")

    # --- Proyectos -------------------------------------------------------------
    if reporte["proyectos_en_curso"]:
        titulo("Proyectos activos o en recaudación de fondos")
        anchos = [ancho * 0.36, ancho * 0.17, ancho * 0.17, ancho * 0.17, ancho * 0.13]
        filas = [
            [
                (p["nombre"][:42], None),
                (dinero(p["monto_total"]), None),
                (dinero(p["recaudado"]), None),
                (dinero(p["falta"]), None),
                (f"{p['avance']}%", None),
            ]
            for p in reporte["proyectos_en_curso"]
        ]
        tabla(["Proyecto", "Monto total", "Recaudado", "Falta", "Avance"],
              anchos, filas, ["L", "R", "R", "R", "R"])
        nota("«Avance» es el avance de recaudación: cuánto se lleva recaudado del monto total del proyecto, no el avance de la obra o el trámite.")

    # --- Flujo de caja ------------------------------------------------------------
    titulo(f"Estado de flujo de caja — {reporte['periodo']}")
    anchos = [ancho * 0.7, ancho * 0.3]
    filas = [
        [("Saldo de apertura", None), (dinero(reporte["saldo_apertura"]), None)],
        [("Más: ingresos del mes", None), (dinero(reporte["ingresos_reales"]), None)],
    ]
    for e in reporte["egresos_mes"]:
        filas.append([(f"     Menos: {e.concepto[:60]}", GRIS), (dinero(e.monto), GRIS)])
    filas.append([("Menos: total de egresos", None), (dinero(reporte["total_egresos"]), None)])
    tabla(["Concepto", "Monto"], anchos, filas, ["L", "R"],
          total=["Saldo al cierre", dinero(reporte["saldo_cierre"])])

    return bytes(pdf.output())


def nombre_archivo_pdf(conjunto, reporte: dict) -> str:
    limpio = "".join(c for c in conjunto.nombre if c.isalnum() or c in " -_").strip() or "Conjunto"
    return f"Reporte {reporte['periodo']} - {limpio}.pdf"
