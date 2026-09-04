"""Reporte mensual del conjunto.

El reporte es **de un mes cerrado**, no una foto de hoy. Esa es la diferencia
de fondo con la versión anterior: aquí no se reporta "lo que va del mes" sino
un mes que ya terminó y cuyas cifras ya no se van a mover.

La cuenta del dinero es una sola y corre desde que se abrió la cuenta:

    saldo de apertura del mes
      + ingresos reales del mes   (lo que de verdad pagaron las propiedades)
      − egresos del mes
      = saldo al cierre del mes

y el saldo al cierre de un mes *es* el saldo de apertura del siguiente. El
primer saldo de apertura de todos es el que se capturó en el alta del conjunto
(`conjunto.saldo_inicial`).

Junto a las cifras reales va un bloque **teórico** (usado internamente para la
cartera): los mismos renglones pero suponiendo que todas las propiedades
pagaran en tiempo y forma. Desde la ronda 5 este bloque ya no se muestra en el
reporte que ve el administrador —confundía más de lo que ayudaba— pero el
cálculo se conserva porque otras partes de la cartera lo siguen usando.
"""
import calendar
import datetime as dt

from ..models import Conjunto, CONCEPTOS_PAGO
from .cartera import cuota_teorica_del_mes, estado_conjunto, orden_natural
from .formato import mes_largo, mes_titulo


def _mes_anterior(anio: int, mes: int) -> tuple[int, int]:
    return (anio - 1, 12) if mes == 1 else (anio, mes - 1)


def ultimo_dia(anio: int, mes: int) -> dt.date:
    return dt.date(anio, mes, calendar.monthrange(anio, mes)[1])


def mes_reportable(al_dia: dt.date | None = None) -> tuple[int, int]:
    """El mes más nuevo que se puede reportar: siempre el inmediato anterior.

    Un mes en curso no se reporta. Todavía le pueden entrar pagos y egresos, y
    publicar cifras que van a cambiar es peor que no publicarlas.
    """
    al_dia = al_dia or dt.date.today()
    return _mes_anterior(al_dia.year, al_dia.month)


def meses_disponibles(conjunto: Conjunto, al_dia: dt.date | None = None) -> list[dict]:
    """Todos los meses cerrados desde que arrancó la cuenta, del más nuevo al
    más viejo. Los históricos no caducan: se pueden consultar siempre. Lo único
    que se mueve es cuál es "el nuevo" — durante todo octubre el nuevo es
    septiembre; al entrar noviembre, pasa a serlo octubre.
    """
    al_dia = al_dia or dt.date.today()
    tope_anio, tope_mes = mes_reportable(al_dia)
    inicio = conjunto.fecha_inicio_cobros

    meses = []
    anio, mes = tope_anio, tope_mes
    while (anio, mes) >= (inicio.year, inicio.month):
        meses.append(
            {
                "anio": anio,
                "mes": mes,
                "titulo": mes_titulo(anio, mes),
                "es_el_nuevo": (anio, mes) == (tope_anio, tope_mes),
            }
        )
        anio, mes = _mes_anterior(anio, mes)
        if len(meses) >= 120:
            break
    return meses


def hay_reporte_disponible(conjunto: Conjunto, al_dia: dt.date | None = None) -> bool:
    return bool(meses_disponibles(conjunto, al_dia))


def saldo_acumulado(conjunto: Conjunto, hasta: dt.date | None = None) -> float:
    """El saldo acumulado del conjunto a una fecha: lo que había al abrir la
    cuenta, más todo lo que entró, menos todo lo que salió, hasta ese día."""
    hasta = hasta or dt.date.today()
    ingresos = sum(p.monto for p in conjunto.pagos if p.fecha_recepcion <= hasta)
    egresos = sum(e.monto for e in conjunto.egresos if e.fecha <= hasta)
    return round((conjunto.saldo_inicial or 0.0) + ingresos - egresos, 2)


def saldo_acumulado_teorico(conjunto: Conjunto, hasta: dt.date | None = None) -> float:
    """El mismo saldo, pero en el mundo donde todas las propiedades pagaron su
    mantenimiento cada mes. Los egresos son los reales: el gasto ocurrió,
    pagaran o no."""
    hasta = hasta or dt.date.today()
    inicio = conjunto.fecha_inicio_cobros.replace(day=1)
    cursor = inicio
    ingresos = 0.0
    while cursor <= hasta.replace(day=1):
        if ultimo_dia(cursor.year, cursor.month) <= hasta:
            ingresos += cuota_teorica_del_mes(conjunto, cursor.year, cursor.month)
        cursor = (
            cursor.replace(year=cursor.year + 1, month=1)
            if cursor.month == 12
            else cursor.replace(month=cursor.month + 1)
        )
    egresos = sum(e.monto for e in conjunto.egresos if e.fecha <= hasta)
    return round((conjunto.saldo_inicial or 0.0) + ingresos - egresos, 2)


def datos_reporte(
    conjunto: Conjunto,
    anio: int | None = None,
    mes: int | None = None,
    al_dia: dt.date | None = None,
) -> dict:
    al_dia = al_dia or dt.date.today()
    if anio is None or mes is None:
        anio, mes = mes_reportable(al_dia)

    primero = dt.date(anio, mes, 1)
    ultimo = ultimo_dia(anio, mes)
    dia_previo = primero - dt.timedelta(days=1)

    pagos_mes = sorted(
        [p for p in conjunto.pagos if primero <= p.fecha_recepcion <= ultimo],
        key=lambda p: (p.fecha_recepcion, p.id),
    )
    egresos_mes = sorted(
        [e for e in conjunto.egresos if primero <= e.fecha <= ultimo],
        key=lambda e: (e.fecha, e.id),
    )

    ingresos_reales = round(sum(p.monto for p in pagos_mes), 2)
    total_egresos = round(sum(e.monto for e in egresos_mes), 2)
    flujo_caja = round(ingresos_reales - total_egresos, 2)

    saldo_apertura = saldo_acumulado(conjunto, dia_previo)
    saldo_cierre = round(saldo_apertura + flujo_caja, 2)

    ingresos_teoricos = cuota_teorica_del_mes(conjunto, anio, mes)
    flujo_teorico = round(ingresos_teoricos - total_egresos, 2)
    saldo_teorico_apertura = saldo_acumulado_teorico(conjunto, dia_previo)
    saldo_teorico_cierre = round(saldo_teorico_apertura + flujo_teorico, 2)

    estado = estado_conjunto(conjunto, ultimo)
    pagado_por_propiedad = {}
    for p in pagos_mes:
        pagado_por_propiedad[p.propiedad_id] = round(
            pagado_por_propiedad.get(p.propiedad_id, 0.0) + p.monto, 2
        )
    for e in estado:
        e["pagado_en_el_mes"] = pagado_por_propiedad.get(e["propiedad"].id, 0.0)

    cartera_total = round(sum(e["saldo"] for e in estado if e["saldo"] > 0), 2)
    al_corriente = sum(1 for e in estado if e["al_corriente"])
    con_adeudo = sum(1 for e in estado if not e["al_corriente"])

    ingresos_por_concepto = []
    for clave, etiqueta in CONCEPTOS_PAGO:
        monto = round(sum(p.monto for p in pagos_mes if p.concepto == clave), 2)
        if monto:
            ingresos_por_concepto.append({"concepto": etiqueta, "monto": monto})

    proyectos_en_curso = []
    for proyecto in sorted(conjunto.proyectos, key=lambda x: (x.fecha_alta, x.id)):
        if not proyecto.en_curso or proyecto.fecha_alta > ultimo:
            continue
        total = round(proyecto.monto_total, 2)
        recaudado = round(
            sum(p.monto for p in proyecto.pagos if p.fecha_recepcion <= ultimo), 2
        )
        proyectos_en_curso.append(
            {
                "proyecto": proyecto,
                "nombre": proyecto.concepto,
                "monto_total": total,
                "recaudado": recaudado,
                "falta": round(max(total - recaudado, 0.0), 2),
                "avance": round(100 * recaudado / total, 1) if total else 0.0,
            }
        )

    disponibles = meses_disponibles(conjunto, al_dia)
    es_el_nuevo = bool(disponibles) and (anio, mes) == (
        disponibles[0]["anio"],
        disponibles[0]["mes"],
    )

    return {
        "conjunto": conjunto,
        "generado_en": dt.datetime.now(),
        "anio": anio,
        "mes": mes,
        "periodo": mes_titulo(anio, mes),
        "periodo_largo": mes_largo(anio, mes),
        "primer_dia": primero,
        "ultimo_dia": ultimo,
        "es_el_nuevo": es_el_nuevo,
        "meses_disponibles": disponibles,
        "saldo_apertura": saldo_apertura,
        "saldo_cierre": saldo_cierre,
        "ingresos_reales": ingresos_reales,
        "total_egresos": total_egresos,
        "flujo_caja": flujo_caja,
        "egresos_mes": egresos_mes,
        "pagos_mes": pagos_mes,
        "ingresos_por_concepto": ingresos_por_concepto,
        "ingresos_teoricos": ingresos_teoricos,
        "flujo_teorico": flujo_teorico,
        "saldo_teorico_apertura": saldo_teorico_apertura,
        "saldo_teorico_cierre": saldo_teorico_cierre,
        "diferencia_por_morosidad": round(ingresos_teoricos - ingresos_reales, 2),
        "cuota_del_mes": conjunto.monto_vigente_en(primero),
        "propiedades_activas": len([p for p in conjunto.propiedades if p.activo]),
        "estado_propiedades": estado,
        "cartera_total": cartera_total,
        "propiedades_al_corriente": al_corriente,
        "propiedades_con_adeudo": con_adeudo,
        "proyectos_en_curso": proyectos_en_curso,
    }


def resumen_actual(conjunto: Conjunto, al_dia: dt.date | None = None) -> dict:
    """Cifras de hoy para la pantalla de Inicio. No es el reporte: es el estado
    del conjunto en este momento, con el mes en curso todavía abierto."""
    al_dia = al_dia or dt.date.today()
    estado = estado_conjunto(conjunto, al_dia)
    inicio_mes = al_dia.replace(day=1)
    return {
        "saldo_acumulado": saldo_acumulado(conjunto, al_dia),
        "ingresos_mes": round(
            sum(p.monto for p in conjunto.pagos if p.fecha_recepcion >= inicio_mes), 2
        ),
        "egresos_mes": round(
            sum(e.monto for e in conjunto.egresos if e.fecha >= inicio_mes), 2
        ),
        "cartera_total": round(sum(e["saldo"] for e in estado if e["saldo"] > 0), 2),
        "propiedades_al_corriente": sum(1 for e in estado if e["al_corriente"]),
        "propiedades_con_adeudo": sum(1 for e in estado if not e["al_corriente"]),
        "estado_propiedades": estado,
    }
