"""Cálculo del estado de cuenta / cartera por propiedad.

Dos reglas de negocio viven aquí y conviene tenerlas presentes:

1. **Solo Mantenimiento y Proyecto abonan a la deuda.** Los pagos de Gas,
   Agua y Otros se registran como ingreso del conjunto (entran a la caja
   chica y al reporte) pero no reducen el saldo de la propiedad, porque la
   cartera únicamente cobra mantenimiento y proyectos. Si un pago de gas
   bajara la deuda, un vecino aparecería "al corriente" sin haber pagado su
   mantenimiento.

2. **Los pagos se aplican al adeudo más viejo primero**, como los bancos. El
   administrador no declara a qué mes corresponde un pago: se captura el
   concepto y el sistema lo acomoda. Un pago de proyecto va primero contra
   ese proyecto; lo que sobre baja al adeudo más viejo que quede.
"""
import datetime as dt
import re

from ..models import Conjunto, Propiedad
from .formato import estado_saldo


def _sumar_mes(fecha: dt.date) -> dt.date:
    if fecha.month == 12:
        return fecha.replace(year=fecha.year + 1, month=1)
    return fecha.replace(month=fecha.month + 1)


def _restar_mes(fecha: dt.date) -> dt.date:
    if fecha.month == 1:
        return fecha.replace(year=fecha.year - 1, month=12)
    return fecha.replace(month=fecha.month - 1)


def esperado_mantenimiento_a_la_fecha(conjunto: Conjunto, hasta: dt.date) -> float:
    """Suma, mes a mes desde que arrancó la cuenta hasta `hasta`, el monto de
    mantenimiento que estaba vigente en cada mes. Respeta los cambios
    históricos de monto (un cambio solo aplica hacia adelante). Aproximación
    por mes calendario completo (no prorratea por día).

    El mes de `hasta` se incluye **solo si ya pasó la fecha límite de pago**.
    Antes de esa fecha el mes en curso todavía no se debe, y contarlo pondría a
    todo el conjunto en morosidad cada día primero.
    """
    inicio = conjunto.fecha_inicio_cobros.replace(day=1)
    limite = hasta.replace(day=1)
    if not conjunto.cargo_del_mes_es_exigible(hasta.year, hasta.month, hasta):
        limite = _restar_mes(limite)
    if limite < inicio:
        return 0.0

    total = 0.0
    cursor = inicio
    while cursor <= limite:
        total += conjunto.monto_vigente_en(cursor)
        cursor = _sumar_mes(cursor)
    return total


def cuota_teorica_del_mes(conjunto: Conjunto, anio: int, mes: int) -> float:
    """Lo que debería entrar de mantenimiento en ese mes si todas las
    propiedades activas pagaran: número de propiedades por la cuota que estaba
    vigente **ese** mes, no la de hoy. Si la cuota subió de $500 a $750 en
    enero, diciembre se calcula con $500 y enero con $750."""
    activas = [p for p in conjunto.propiedades if p.activo]
    if not activas:
        return 0.0
    if dt.date(anio, mes, 1) < conjunto.fecha_inicio_cobros.replace(day=1):
        return 0.0
    return round(len(activas) * conjunto.monto_vigente_en(dt.date(anio, mes, 1)), 2)


def orden_natural(propiedad: Propiedad):
    """Ordena 2 antes que 10, y A1 antes que B3. El número es texto libre
    (101, A1, PB-2), así que un orden alfabético a secas pondría el 10 antes
    del 2 — que es justo lo que confunde al leer una lista de vecinos.

    Cada trozo se envuelve en una tupla con su tipo delante. Sin eso, comparar
    "101" con "A1" acaba comparando un entero contra un texto y Python levanta
    las manos — que es justo lo que pasaba en cuanto un conjunto mezclaba
    numeración de departamentos con cajones de estacionamiento.
    """
    txt = (propiedad.numero or "").strip()
    clave = []
    for parte in re.split(r"(\d+)", txt):
        if parte == "":
            continue
        if parte.isdigit():
            clave.append((0, int(parte), ""))
        else:
            clave.append((1, 0, parte.lower()))
    return clave


def _propiedad_que_absorbe_redondeo(conjunto: Conjunto):
    """La propiedad a la que se le ajusta el centavo de diferencia cuando
    monto_total / num_propiedades no da un número exacto, para que la suma
    de todas las propiedades cuadre con el monto_total del proyecto."""
    activas = [p for p in conjunto.propiedades if p.activo]
    if not activas:
        return None
    return max(activas, key=lambda p: p.id)


def detalle_proyectos(propiedad: Propiedad, hasta: dt.date | None = None) -> list[dict]:
    """Cargo esperado por cada proyecto/cuota extraordinaria vigente,
    etiquetado con el nombre (concepto) del proyecto.

    Con `hasta` se ignoran los proyectos dados de alta después de esa fecha:
    el reporte de septiembre no puede cobrar un proyecto que nació en octubre.
    """
    conjunto = propiedad.conjunto
    activas = [p for p in conjunto.propiedades if p.activo]
    n = len(activas)
    ajuste = _propiedad_que_absorbe_redondeo(conjunto)

    detalle = []
    for proyecto in sorted(conjunto.proyectos, key=lambda x: (x.fecha_alta, x.id)):
        if hasta is not None and proyecto.fecha_alta > hasta:
            continue
        if n == 0:
            monto = 0.0
        elif ajuste is not None and propiedad.id == ajuste.id:
            monto = proyecto.monto_total - proyecto.monto_por_propiedad * (n - 1)
        else:
            monto = proyecto.monto_por_propiedad
        detalle.append(
            {
                "proyecto_id": proyecto.id,
                "nombre": proyecto.concepto,
                "monto": round(monto, 2),
            }
        )
    return detalle


def cargo_esperado_proyectos(propiedad: Propiedad) -> float:
    return sum(d["monto"] for d in detalle_proyectos(propiedad))


# ---------------------------------------------------------------------------
# Aplicación de pagos a los cargos (del más viejo al más nuevo)
# ---------------------------------------------------------------------------

def _aplicar(cargos: list[dict], monto: float, clave_preferida: str | None = None) -> float:
    """Aplica `monto` a los cargos pendientes y devuelve lo que haya sobrado
    (que se vuelve saldo a favor de la propiedad).

    Si se indica `clave_preferida`, ese cargo se cubre primero — es el caso
    de un pago marcado para un proyecto específico. El resto siempre se
    aplica al adeudo más viejo que siga pendiente.
    """
    restante = round(monto, 2)

    if clave_preferida:
        for c in cargos:
            if c["clave"] == clave_preferida:
                aplica = min(restante, max(round(c["monto"] - c["pagado"], 2), 0.0))
                c["pagado"] = round(c["pagado"] + aplica, 2)
                restante = round(restante - aplica, 2)
                break

    for c in cargos:
        if restante <= 0:
            break
        aplica = min(restante, max(round(c["monto"] - c["pagado"], 2), 0.0))
        c["pagado"] = round(c["pagado"] + aplica, 2)
        restante = round(restante - aplica, 2)

    return max(restante, 0.0)


def _cargos_de(propiedad: Propiedad, esperado_mantenimiento: float, detalle_py: list[dict]) -> list[dict]:
    """Los cargos de la propiedad, ordenados del más viejo al más nuevo:
    primero lo que ya debía al entrar al sistema, luego el mantenimiento
    acumulado, y al final los proyectos por orden de alta."""
    cargos = []
    saldo_inicial = round(propiedad.saldo_inicial, 2)

    if saldo_inicial > 0:
        cargos.append(
            {"clave": "saldo_inicial", "nombre": "Saldo inicial", "monto": saldo_inicial, "pagado": 0.0}
        )

    cargos.append(
        {
            "clave": "mantenimiento",
            "nombre": "Mantenimiento",
            "monto": round(esperado_mantenimiento, 2),
            "pagado": 0.0,
        }
    )

    for d in detalle_py:
        cargos.append(
            {
                "clave": f"proyecto:{d['proyecto_id']}",
                "nombre": d["nombre"],
                "monto": d["monto"],
                "pagado": 0.0,
                "proyecto_id": d["proyecto_id"],
            }
        )

    return cargos


def estado_propiedad(propiedad: Propiedad, hasta: dt.date | None = None) -> dict:
    hasta = hasta or dt.date.today()
    conjunto = propiedad.conjunto

    esperado_mantenimiento = esperado_mantenimiento_a_la_fecha(conjunto, hasta)
    detalle_py = detalle_proyectos(propiedad, hasta)
    esperado_proyectos = sum(d["monto"] for d in detalle_py)

    cargos = _cargos_de(propiedad, esperado_mantenimiento, detalle_py)

    a_favor = 0.0
    saldo_inicial = round(propiedad.saldo_inicial, 2)
    if saldo_inicial < 0:
        a_favor += _aplicar(cargos, -saldo_inicial)

    hasta_la_fecha = [p for p in propiedad.pagos if p.fecha_recepcion <= hasta]
    pagos_abonables = sorted(
        [p for p in hasta_la_fecha if p.abona_a_cartera],
        key=lambda p: (p.fecha_recepcion, p.id),
    )
    for pago in pagos_abonables:
        clave = (
            f"proyecto:{pago.proyecto_id}"
            if pago.concepto == "proyecto" and pago.proyecto_id
            else None
        )
        a_favor += _aplicar(cargos, pago.monto, clave)

    pagado_mantenimiento = sum(
        c["pagado"] for c in cargos if c["clave"] in ("saldo_inicial", "mantenimiento")
    )
    detalle_final = []
    for c in cargos:
        if not c["clave"].startswith("proyecto:"):
            continue
        detalle_final.append(
            {
                "proyecto_id": c["proyecto_id"],
                "nombre": c["nombre"],
                "monto": c["monto"],
                "pagado": c["pagado"],
                "saldo": round(c["monto"] - c["pagado"], 2),
            }
        )

    pendiente = round(sum(c["monto"] - c["pagado"] for c in cargos), 2)
    saldo = round(pendiente - a_favor, 2)

    total_esperado = round(sum(c["monto"] for c in cargos), 2)
    total_abonado = round(sum(p.monto for p in pagos_abonables), 2)
    otros_ingresos = round(
        sum(p.monto for p in hasta_la_fecha if not p.abona_a_cartera), 2
    )

    return {
        "propiedad": propiedad,
        "esperado_mantenimiento": round(esperado_mantenimiento, 2),
        "pagado_mantenimiento": round(pagado_mantenimiento, 2),
        "saldo_mantenimiento": round(
            sum(
                c["monto"] - c["pagado"]
                for c in cargos
                if c["clave"] in ("saldo_inicial", "mantenimiento")
            ),
            2,
        ),
        "esperado_proyectos": round(esperado_proyectos, 2),
        "detalle_proyectos": detalle_final,
        "saldo_inicial": saldo_inicial,
        "total_esperado": total_esperado,
        "total_pagado": total_abonado,
        "otros_ingresos": otros_ingresos,
        "saldo": saldo,
        "al_corriente": saldo <= 0,
        "vista": estado_saldo(saldo),
    }


def estado_conjunto(conjunto: Conjunto, hasta: dt.date | None = None) -> list[dict]:
    return [
        estado_propiedad(p, hasta)
        for p in sorted(conjunto.propiedades, key=orden_natural)
        if p.activo
    ]
