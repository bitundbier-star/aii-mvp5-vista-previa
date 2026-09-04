"""Conjunto de ejemplo para las demostraciones.

Por qué existe: en el plan gratuito de Render la base de datos vive en un disco
temporal y se borra sola cada vez que el servicio se duerme o se redespliega.
Para una demostración eso es fatal — el cliente potencial entra y encuentra la
herramienta vacía, sin cartera, sin reporte, sin nada que mirar.

La solución es que la aplicación se siembre a sí misma: si al arrancar no
encuentra el conjunto de ejemplo, lo vuelve a crear. Así el borrado se vuelve
invisible y la demostración siempre se ve llena.

Se activa con la variable de entorno MODO_DEMO=1. Apagada —que es lo normal—
esto no corre nunca y no toca nada.

El conjunto de ejemplo está armado para que se vean las situaciones reales de
un condominio, no un caso ideal: alguien que debe varios meses, alguien que
pagó por adelantado, alguien que paga de a poquito, un proyecto a medio
recaudar y una cuota que subió a mitad del año.
"""
import datetime as dt
import os
import random

from passlib.hash import bcrypt

from . import models
from .database import SessionLocal

MODO_DEMO = os.environ.get("MODO_DEMO", "").strip() in ("1", "true", "si", "sí")

DEMO_EMAIL = os.environ.get("DEMO_EMAIL", "demo@aii.mx")
# Configurable para poder cerrar el acceso sin tocar código: se cambia la
# variable de entorno, el servicio se redespliega y la contraseña anterior deja
# de servir.
DEMO_PASSWORD = os.environ.get("DEMO_PASSWORD", "demo1234")
DEMO_NOMBRE = os.environ.get("DEMO_NOMBRE", "Privada Las Jacarandas")

MESES_DE_HISTORIA = 7


def _sumar_mes(fecha: dt.date) -> dt.date:
    if fecha.month == 12:
        return fecha.replace(year=fecha.year + 1, month=1)
    return fecha.replace(month=fecha.month + 1)


def _restar_meses(fecha: dt.date, meses: int) -> dt.date:
    total = fecha.year * 12 + (fecha.month - 1) - meses
    return dt.date(total // 12, total % 12 + 1, 1)


# (número, tipo, propietario, celular, correo, residente, saldo inicial,
#  perfil de pago)
#
# El perfil decide cómo se porta cada vecino mes con mes. Es lo que hace que la
# cartera se vea como una de verdad y no como una tabla de ceros.
VECINOS = [
    ("101", "departamento", "Ana Ruiz Delgado", "+52 55 1234 5678",
     "ana.ruiz@ejemplo.com", "Luis Ruiz Ontiveros", 0.0, "puntual"),
    ("102", "departamento", "Carlos Mendoza Lara", "+52 55 2233 4455",
     "carlos.mendoza@ejemplo.com", "", -1500.0, "adelantado"),
    ("103", "departamento", "Beatriz Ontiveros Vega", "+52 55 3344 5566",
     "beatriz.ontiveros@ejemplo.com", "Marta Solís", 1500.0, "moroso"),
    ("104", "departamento", "Jorge Salinas Vega", "+52 55 4455 6677",
     "", "", 0.0, "puntual"),
    ("201", "departamento", "Renata Cházaro Puig", "",
     "renata.chazaro@ejemplo.com", "", 0.0, "irregular"),
    ("202", "departamento", "Guillermo Arriaga Sosa", "+52 55 6677 8899",
     "memo.arriaga@ejemplo.com", "Familia Bermúdez", 0.0, "puntual"),
    ("PB-1", "local", "Rodrigo Fuentes Nava", "+52 55 7788 9900",
     "rfuentes@ejemplo.com", "Cafetería La Esquina", 0.0, "puntual"),
    ("E-1", "estacionamiento", "Ana Ruiz Delgado", "+52 55 1234 5678",
     "ana.ruiz@ejemplo.com", "", 0.0, "irregular"),
]

EGRESOS_FIJOS = [
    ("Jardinería", 1400.0),
    ("Luz de áreas comunes", 2200.0),
    ("Internet de caseta", 650.0),
    ("Mantenimiento del elevador", 2800.0),
]

EGRESOS_EVENTUALES = [
    ("Reparación de la bomba de agua", 4300.0),
    ("Recarga de extintores", 1650.0),
    ("Pintura de la reja principal", 3900.0),
    ("Cambio de luminarias del pasillo", 2100.0),
    ("Fumigación de áreas comunes", 1800.0),
]


def _paga_este_mes(perfil: str, cuota: float, indice: int, azar: random.Random):
    """Cuánto paga este vecino en el mes número `indice`. None = no pagó."""
    if perfil == "puntual":
        return cuota
    if perfil == "adelantado":
        return cuota * 2 if indice % 3 == 0 else cuota
    if perfil == "moroso":
        if indice % 3 != 1:
            return None
        return round(cuota * azar.choice([0.5, 0.6, 1.0]), 2)
    if perfil == "irregular":
        if azar.random() < 0.25:
            return None
        return round(cuota * azar.choice([0.7, 1.0, 1.0, 1.3]), 2)
    return cuota


def sembrar_si_hace_falta():
    """Crea el conjunto de ejemplo si no existe. Es seguro llamarla siempre."""
    if not MODO_DEMO:
        return

    db = SessionLocal()
    try:
        ya_esta = db.query(models.Conjunto).filter_by(login_email=DEMO_EMAIL).first()
        if ya_esta:
            return
        _sembrar(db)
        db.commit()
        print(f"[demo] Conjunto de ejemplo creado: {DEMO_EMAIL} / {DEMO_PASSWORD}")
    except Exception as exc:  # pragma: no cover
        db.rollback()
        print(f"[demo] No se pudo crear el conjunto de ejemplo: {exc}")
    finally:
        db.close()


def _sembrar(db):
    azar = random.Random(20260901)  # siempre los mismos datos
    hoy = dt.date.today()
    inicio = _restar_meses(hoy, MESES_DE_HISTORIA)

    cuota_vieja, cuota_nueva = 1500.0, 1800.0
    cambio_cuota = _restar_meses(hoy, MESES_DE_HISTORIA // 2)

    conjunto = models.Conjunto(
        nombre=DEMO_NOMBRE,
        direccion="Av. de las Jacarandas 45, Col. Ejemplo, Ciudad de México",
        admin_nombre="Sofía Bellinghausen",
        login_email=DEMO_EMAIL,
        password_hash=bcrypt.hash(DEMO_PASSWORD),
        cuenta_email="cuenta.jacarandas@ejemplo.com",
        correo_recuperacion="subadmin.jacarandas@ejemplo.com",
        monto_mensual=cuota_nueva,
        monto_revision_meses=12,
        fecha_limite_pago=11,
        saldo_inicial=18400.0,
        fecha_inicio_cobros=inicio,
        monto_confirmado_en=inicio,
    )
    db.add(conjunto)
    db.flush()

    db.add(models.MontoMensual(conjunto_id=conjunto.id, monto=cuota_vieja, vigente_desde=inicio))
    db.add(models.MontoMensual(conjunto_id=conjunto.id, monto=cuota_nueva, vigente_desde=cambio_cuota))

    propiedades = []
    for numero, tipo, dueno, celular, correo, residente, saldo, perfil in VECINOS:
        p = models.Propiedad(
            conjunto_id=conjunto.id,
            numero=numero,
            tipo=tipo,
            nombre_dueno=dueno,
            celular_dueno=celular or None,
            email_dueno=correo or None,
            nombre_residente=residente or None,
            notas="N/A",
            saldo_inicial=saldo,
        )
        db.add(p)
        propiedades.append((p, perfil))
    db.flush()

    n = len(propiedades)
    impermeabilizacion = models.Proyecto(
        conjunto_id=conjunto.id,
        concepto="Impermeabilización de azoteas",
        descripcion="Las cuatro azoteas del conjunto, con garantía de 5 años.",
        monto_total=48000.0,
        monto_por_propiedad=round(48000.0 / n, 2),
        fecha_alta=_restar_meses(hoy, MESES_DE_HISTORIA - 2),
        fecha_limite_pago=_sumar_mes(hoy),
        estado="en_recaudacion",
        comentario_estado="Ya se juntaron tres cotizaciones; falta decidir proveedor.",
    )
    camaras = models.Proyecto(
        conjunto_id=conjunto.id,
        concepto="Cámaras de vigilancia en accesos",
        descripcion="Cuatro cámaras y un mes de grabación en la nube.",
        monto_total=22000.0,
        monto_por_propiedad=round(22000.0 / n, 2),
        fecha_alta=_restar_meses(hoy, 1),
        estado="por_iniciar",
    )
    db.add_all([impermeabilizacion, camaras])
    db.flush()

    pagado_proyecto = {}

    cursor = inicio
    while cursor < hoy.replace(day=1):
        cuota = cuota_vieja if cursor < cambio_cuota else cuota_nueva
        indice = (cursor.year * 12 + cursor.month) - (inicio.year * 12 + inicio.month)

        for propiedad, perfil in propiedades:
            monto = _paga_este_mes(perfil, cuota, indice, azar)
            if not monto:
                continue
            dia = azar.randint(2, 14)
            fecha = cursor.replace(day=min(dia, 28))
            conjunto.ultimo_folio += 1
            db.add(models.Pago(
                conjunto_id=conjunto.id,
                propiedad_id=propiedad.id,
                folio=f"AII-{conjunto.id:04d}-{conjunto.ultimo_folio:05d}",
                fecha_recepcion=fecha,
                monto=round(monto, 2),
                concepto="mantenimiento",
                metodo_pago=azar.choice(["efectivo", "deposito", "efectivo", "cheque"]),
            ))

            if indice >= 3 and perfil in ("puntual", "adelantado"):
                for proyecto in (impermeabilizacion, camaras):
                    if proyecto.fecha_alta > fecha:
                        continue
                    clave = (propiedad.id, proyecto.id)
                    aportado = pagado_proyecto.get(clave, 0.0)
                    falta = round(proyecto.monto_por_propiedad - aportado, 2)
                    if falta <= 0:
                        continue
                    abono = min(falta, round(proyecto.monto_por_propiedad / 3, 2))
                    pagado_proyecto[clave] = aportado + abono
                    conjunto.ultimo_folio += 1
                    db.add(models.Pago(
                        conjunto_id=conjunto.id,
                        propiedad_id=propiedad.id,
                        proyecto_id=proyecto.id,
                        folio=f"AII-{conjunto.id:04d}-{conjunto.ultimo_folio:05d}",
                        fecha_recepcion=fecha,
                        monto=abono,
                        concepto="proyecto",
                        metodo_pago="deposito",
                    ))
                    break
            elif perfil == "irregular" and indice >= 4 and azar.random() < 0.4:
                conjunto.ultimo_folio += 1
                db.add(models.Pago(
                    conjunto_id=conjunto.id,
                    propiedad_id=propiedad.id,
                    proyecto_id=impermeabilizacion.id,
                    folio=f"AII-{conjunto.id:04d}-{conjunto.ultimo_folio:05d}",
                    fecha_recepcion=fecha,
                    monto=round(impermeabilizacion.monto_por_propiedad / 4, 2),
                    concepto="proyecto",
                    metodo_pago="efectivo",
                ))

        for _ in range(azar.randint(1, 3)):
            propiedad, _perfil = azar.choice(propiedades)
            conjunto.ultimo_folio += 1
            db.add(models.Pago(
                conjunto_id=conjunto.id,
                propiedad_id=propiedad.id,
                folio=f"AII-{conjunto.id:04d}-{conjunto.ultimo_folio:05d}",
                fecha_recepcion=cursor.replace(day=azar.randint(5, 25)),
                monto=float(azar.choice([320, 450, 510, 280])),
                concepto=azar.choice(["gas", "agua"]),
                metodo_pago="efectivo",
            ))

        for concepto, monto in EGRESOS_FIJOS:
            db.add(models.Egreso(
                conjunto_id=conjunto.id,
                concepto=concepto,
                monto=round(monto * azar.uniform(0.92, 1.08), 2),
                fecha=cursor.replace(day=azar.randint(3, 27)),
            ))
        if azar.random() < 0.6:
            concepto, monto = azar.choice(EGRESOS_EVENTUALES)
            db.add(models.Egreso(
                conjunto_id=conjunto.id,
                concepto=concepto,
                monto=monto,
                fecha=cursor.replace(day=azar.randint(3, 27)),
            ))

        cursor = _sumar_mes(cursor)

    for propiedad, perfil in propiedades[:4]:
        if perfil == "moroso":
            continue
        conjunto.ultimo_folio += 1
        db.add(models.Pago(
            conjunto_id=conjunto.id,
            propiedad_id=propiedad.id,
            folio=f"AII-{conjunto.id:04d}-{conjunto.ultimo_folio:05d}",
            fecha_recepcion=hoy.replace(day=min(hoy.day, 5)),
            monto=cuota_nueva,
            concepto="mantenimiento",
            metodo_pago="deposito",
        ))
    db.add(models.Egreso(
        conjunto_id=conjunto.id,
        concepto="Jardinería",
        monto=1400.0,
        fecha=hoy.replace(day=min(hoy.day, 3)),
    ))

    db.flush()
    from .services.cartera import estado_propiedad

    ultimo_mes_cerrado = _restar_meses(hoy, 1)
    for propiedad, perfil in propiedades:
        if perfil != "puntual":
            continue
        saldo = estado_propiedad(propiedad)["saldo"]
        if saldo <= 0.01:
            continue
        conjunto.ultimo_folio += 1
        db.add(models.Pago(
            conjunto_id=conjunto.id,
            propiedad_id=propiedad.id,
            folio=f"AII-{conjunto.id:04d}-{conjunto.ultimo_folio:05d}",
            fecha_recepcion=ultimo_mes_cerrado.replace(day=9),
            monto=round(saldo, 2),
            concepto="mantenimiento",
            metodo_pago="deposito",
        ))
