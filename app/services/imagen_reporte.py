"""Reporte mensual como imagen PNG, para poder compartirlo por WhatsApp.

La imagen se genera bajo demanda cuando el administrador la pide. El envío
por WhatsApp *nunca* es automático: la app solo abre WhatsApp con un mensaje
ya escrito y el administrador decide a quién mandárselo y adjunta la imagen.

Aquí rige la misma regla de toda la app: **ninguna cifra lleva signo
negativo**. Un saldo a favor sale en positivo y en verde, con la palabra al
lado. La imagen es justo la que circula por WhatsApp entre los vecinos, así
que es el peor lugar para un número que se lea al revés.
"""
import os

from PIL import Image, ImageDraw

from .imagen import fuente, pegar_logo
from .formato import dinero, estado_saldo, mes_titulo

STATIC_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "static", "reportes"
)
os.makedirs(STATIC_DIR, exist_ok=True)

ANCHO = 1000
MARGEN = 60
VERDE = "#0a7d3c"
ROJO = "#b3261e"
GRIS = "#777777"
TINTA = "#1a1a1a"
LINEA = "#dddddd"

MESES = {
    1: "enero", 2: "febrero", 3: "marzo", 4: "abril", 5: "mayo", 6: "junio",
    7: "julio", 8: "agosto", 9: "septiembre", 10: "octubre", 11: "noviembre",
    12: "diciembre",
}


def periodo_en_espanol(fecha) -> str:
    """Se conserva por compatibilidad con código que aún la llame con una
    fecha suelta. El reporte ya trae su propio periodo formateado."""
    return f"{MESES[fecha.month].capitalize()} {fecha.year}"


def generar_imagen_reporte(conjunto, reporte: dict) -> str:
    """Dibuja el reporte de un mes y devuelve la ruta relativa dentro de static/."""
    generado = reporte["generado_en"]

    egresos = reporte["egresos_mes"][:8]
    proyectos = reporte["proyectos_en_curso"]
    alto = 1000 + 34 * len(egresos) + 44 * len(proyectos)

    img = Image.new("RGB", (ANCHO, alto), color="#ffffff")
    draw = ImageDraw.Draw(img)

    f_titulo = fuente(36, negrita=True)
    f_sub = fuente(22)
    f_seccion = fuente(22, negrita=True)
    f_label = fuente(19)
    f_valor = fuente(26, negrita=True)
    f_grande = fuente(46, negrita=True)
    f_medio = fuente(28, negrita=True)
    f_pie = fuente(15)

    col2 = ANCHO // 2 + 20
    der = ANCHO - MARGEN

    def linea(y, grosor=2, color=LINEA):
        draw.line((MARGEN, y, der, y), fill=color, width=grosor)

    def derecha(texto, y, font, fill):
        ancho = draw.textlength(texto, font=font)
        draw.text((der - ancho, y), texto, font=font, fill=fill)

    y = MARGEN
    pegar_logo(img, MARGEN, y, alto=48)
    y += 76

    draw.text((MARGEN, y), conjunto.nombre, font=f_titulo, fill=TINTA)
    y += 48
    draw.text(
        (MARGEN, y),
        f"Reporte de {reporte['periodo']} · Administra {conjunto.admin_nombre}",
        font=f_sub, fill=GRIS,
    )
    y += 46
    linea(y)
    y += 34

    ancho_caja = (der - MARGEN - 20) // 2
    draw.rounded_rectangle((MARGEN, y, MARGEN + ancho_caja, y + 118), radius=12,
                           outline=VERDE, width=3, fill="#f2faf5")
    draw.rounded_rectangle((MARGEN + ancho_caja + 20, y, der, y + 118), radius=12,
                           outline="#cbd5e1", width=2)

    draw.text((MARGEN + 22, y + 20), "Saldo al cierre del mes", font=f_label, fill=GRIS)
    draw.text((MARGEN + 22, y + 52), dinero(reporte["saldo_cierre"]), font=f_grande, fill=VERDE)
    draw.text((MARGEN + ancho_caja + 42, y + 20), "Saldo al inicio del mes", font=f_label, fill=GRIS)
    draw.text((MARGEN + ancho_caja + 42, y + 56), dinero(reporte["saldo_apertura"]), font=f_medio, fill=TINTA)
    y += 150

    filas = [
        ("Ingresos reales del mes", dinero(reporte["ingresos_reales"]), VERDE,
         "Egresos del mes", dinero(reporte["total_egresos"]), ROJO),
        ("Flujo de caja del mes", dinero(reporte["flujo_caja"]),
         VERDE if reporte["flujo_caja"] >= 0 else ROJO,
         "Cartera por cobrar", dinero(reporte["cartera_total"]),
         ROJO if reporte["cartera_total"] > 0 else VERDE),
    ]
    for la, va, ca, lb, vb, cb in filas:
        draw.text((MARGEN, y), la, font=f_label, fill=GRIS)
        draw.text((col2, y), lb, font=f_label, fill=GRIS)
        y += 28
        draw.text((MARGEN, y), va, font=f_valor, fill=ca)
        draw.text((col2, y), vb, font=f_valor, fill=cb)
        y += 52

    total_props = reporte["propiedades_al_corriente"] + reporte["propiedades_con_adeudo"]
    draw.text((MARGEN, y), "Propiedades al corriente", font=f_label, fill=GRIS)
    y += 28
    draw.text((MARGEN, y), f"{reporte['propiedades_al_corriente']} de {total_props}",
              font=f_valor, fill=TINTA)
    y += 58

    linea(y)
    y += 28

    draw.text((MARGEN, y), f"Egresos de {reporte['periodo']}", font=f_seccion, fill=TINTA)
    y += 40
    if egresos:
        for e in egresos:
            draw.text((MARGEN, y), e.concepto[:46], font=f_label, fill=TINTA)
            derecha(dinero(e.monto), y, f_label, GRIS)
            y += 34
        if len(reporte["egresos_mes"]) > len(egresos):
            draw.text((MARGEN, y), f"…y {len(reporte['egresos_mes']) - len(egresos)} más",
                      font=f_label, fill=GRIS)
            y += 34
        draw.line((MARGEN, y + 4, der, y + 4), fill=TINTA, width=2)
        y += 16
        draw.text((MARGEN, y), "Total de egresos", font=f_label, fill=TINTA)
        derecha(dinero(reporte["total_egresos"]), y, f_valor, ROJO)
        y += 52
    else:
        draw.text((MARGEN, y), "Ninguno este mes.", font=f_label, fill=GRIS)
        y += 46

    if proyectos:
        draw.text((MARGEN, y), "Proyectos en curso — avance de recaudación", font=f_seccion, fill=TINTA)
        y += 40
        for p in proyectos:
            draw.text((MARGEN, y), p["nombre"][:40], font=f_label, fill=TINTA)
            derecha(f"{dinero(p['recaudado'])} de {dinero(p['monto_total'])}  ({p['avance']}%)",
                    y, f_label, GRIS)
            y += 44
        y += 6

    pie_y = y + 14
    total_alto = pie_y + 72
    draw.line((MARGEN, pie_y, der, pie_y), fill=LINEA, width=1)
    draw.text(
        (MARGEN, pie_y + 16),
        f"Del {reporte['primer_dia'].strftime('%d/%m/%Y')} al {reporte['ultimo_dia'].strftime('%d/%m/%Y')} · "
        f"Generado el {generado.strftime('%d/%m/%Y a las %H:%M')} · Administración Inteligente de Inmuebles",
        font=f_pie, fill="#999999",
    )

    if total_alto < alto:
        img = img.crop((0, 0, ANCHO, total_alto))

    nombre_archivo = (
        f"reporte_{conjunto.id}_{reporte['anio']}{reporte['mes']:02d}"
        f"_{generado.strftime('%Y%m%d_%H%M%S')}.png"
    )
    img.save(os.path.join(STATIC_DIR, nombre_archivo))
    return f"reportes/{nombre_archivo}"
