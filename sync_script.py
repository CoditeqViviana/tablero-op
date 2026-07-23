"""
sync_script.py — Consulta Vtiger y genera latest.json.
Union por nombre de referencia (muchos a muchos).
"""
import os, json, re, requests, pandas as pd
from datetime import datetime
import pytz, warnings
import numpy as np
try:
    from scipy.optimize import linear_sum_assignment
    _SCIPY_OK = True
except ImportError:
    _SCIPY_OK = False
warnings.filterwarnings('ignore')

VTIGER_URL = 'https://coditeq.od2.vtiger.com'
VTIGER_USER = os.environ.get('VTIGER_USER', '')
VTIGER_KEY = os.environ.get('VTIGER_KEY', '')
API_BASE = f"{VTIGER_URL}/restapi/v1/vtiger/default"
BOGOTA = pytz.timezone('America/Bogota')

# ==================== CONSTANTES TOC / CAPACIDAD ====================
# Capacidad: 22.75h * 50% eficiencia * 60min * 35m/min = 23,888 mts/dia
CAP_DEFAULT = round(22.75 * 0.50 * 60 * 35)
CAPACIDAD = {
    'Nilpeter 1': CAP_DEFAULT,
    'Nilpeter 2': CAP_DEFAULT,
    'Kromia':     CAP_DEFAULT,
}

# Capacidades diarias por maquina (metros/dia)
MAQUINAS_CAP = {
    'NP1': 23100, 'NP2': 23100, 'Kromia': 23100,
    'Rebobinadora 1': 16500, 'Rebobinadora 2': 16500,
    'Rebobinadora 3': 16500, 'Rebobinadora (T) 4': 16500,
    'Rebobinadora KM': 16500,
    'Troqueladora 1': 16500, 'Troqueladora 2': 16500,
    'Troqueladora 3': 16500, 'Troqueladora Aut 4': 16500,
    'Troqueladora Plana': 16500, 'Plegadora': 16500,
}
TROT_NAMES = ['Troqueladora 1', 'Troqueladora 2', 'Troqueladora 3', 'Troqueladora Aut 4']
REB_NAMES = ['Rebobinadora 1', 'Rebobinadora 2', 'Rebobinadora 3', 'Rebobinadora (T) 4', 'Rebobinadora KM']

# Amortiguadores y promesas por familia (dias habiles)
FAMILIAS_AMORT = {
    'F1': 2, 'F2': 2, 'F3': 8, 'F4': 1, 'F5': 3, 'F6': 3, 'F7': 4, 'F8': 3,
    'F9': 5, 'F10': 4, 'F11': 5, 'F12': 5, 'F13': 3, 'F14': 3, 'F15': 4,
    'F16': 3, 'F17': 8, 'F18': 8
}
FAMILIAS_PROMESA = {
    'F1': 5, 'F2': 4, 'F3': 16, 'F4': 2, 'F5': 7, 'F6': 6, 'F7': 8, 'F8': 3,
    'F9': 10, 'F10': 9, 'F11': 11, 'F12': 5, 'F13': 7, 'F14': 6, 'F15': 8,
    'F16': 3, 'F17': 16, 'F18': 16
}

ETAPAS_REBOBINADO = {'RM4 - Reboninado', 'En cola Rebobinadora 1', 'En cola Rebobinadora',
                     'Rebobinando', 'Cola rebobinado'}
ETAPAS_TROQUELADO = {'En cola Troqueladora 4', 'En cola Troqueladora', 'Troquelando'}
ETAPAS_IMPRESION = {'Preparacion', 'En cola impresión NP1', 'Impresion',
                    'En cola impresion NP1', 'En cola impresión NP2', 'En cola impresión Kromia'}
ETAPAS_IMP_STD = {'Preparacion', 'En cola impresión NP1', 'Impresion', 'En cola impresion NP1'}
ETAPAS_IMP_SET = {'Preparacion', 'En cola impresión NP1', 'Impresion', 'En cola impresion NP1',
                  'En cola impresión NP2', 'En cola impresión Kromia'}


def get_maquinas_reales(proceso):
    """Devuelve lista de maquinas reales para una orden segun su proceso (texto crudo
    de Centro de Proceso, puede contener varios valores separados por '|##|')."""
    maquinas = []
    p = proceso.upper()
    if 'NILPETER 1' in p: maquinas.append('NP1')
    if 'NILPETER 2' in p: maquinas.append('NP2')
    if 'KROMIA' in p: maquinas.append('Kromia')
    if 'REBOBINADORA' in p and 'MOTEX' not in p:
        maquinas.append('REBOBINADORA_BALANCEAR')
    if 'TROQUELADORA PLANA' in p: maquinas.append('Troqueladora Plana')
    if 'PLEGADORA' in p: maquinas.append('Plegadora')
    return maquinas


def get_tipo(ref):
    r = str(ref).strip().upper()
    if r.startswith('BL'): return 'Blanca'
    if r.startswith('IC') or r.startswith('IS'): return 'Impresa'
    if r.startswith('FD'): return 'Fondo'
    return 'Otro'


def get_color_toc(fecha_creacion, fecha_entrega, today):
    if pd.isna(fecha_entrega):
        return 'gris'
    if today.date() > fecha_entrega.date():
        return 'negro'
    if pd.isna(fecha_creacion):
        return 'rojo'
    # Normalizar a solo-fecha (medianoche) antes de restar: fecha_creacion
    # trae hora (ej. "23-05-2026 05:09 PM") mientras fecha_entrega/today son
    # solo fecha -- restar un datetime con hora contra uno sin hora trunca el
    # resultado de .days 1 dia menos del real (bug reportado: columnas Dias/
    # Color del tablero sistematicamente "menos 1" dia).
    fc_norm = fecha_creacion.normalize()
    fe_norm = fecha_entrega.normalize()
    today_norm = today.normalize()
    duracion = (fe_norm - fc_norm).days
    if duracion <= 0:
        return 'rojo'
    consumido = (today_norm - fc_norm).days
    pct = consumido / duracion
    if pct <= 0.50:
        return 'azul'
    elif pct <= 0.6667:
        return 'verde'
    elif pct <= 0.8333:
        return 'amarillo'
    else:
        return 'rojo'


def sumar_dias_lab(fecha_ini, dias):
    from datetime import timedelta
    f = fecha_ini
    d = 0
    while d < int(dias):
        f += timedelta(days=1)
        if f.weekday() != 6:
            d += 1
    return f


def asignar_maquina(row):
    """Determina la maquina 'display' de una orden: si esta en etapa de rebobinado
    o troquelado se reasigna a esa estacion; si no, se usa la primera maquina
    listada en Centro de Proceso (separador '|##|' de la API de Vtiger)."""
    etapa = str(row['etapa']).strip()
    proceso = str(row['maquina_raw']).strip()
    primera = proceso.split('|##|')[0].strip()
    if etapa in ETAPAS_REBOBINADO or 'RM4' in etapa.upper() or 'REBOB' in etapa.upper():
        return 'Rebobinadora'
    if etapa in ETAPAS_TROQUELADO or 'TROQUELAD' in etapa.upper():
        return 'Troqueladora Rotativa 1'
    return primera

def _auth():
    return (VTIGER_USER, VTIGER_KEY)

def vtiger_query(query_str, page_size=100):
    all_records, offset = [], 0
    while True:
        paged = f"{query_str} LIMIT {offset}, {page_size};"
        try:
            r = requests.get(f"{API_BASE}/query", auth=_auth(),
                           params={'query': paged}, timeout=60, verify=False)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"[vtiger] Error offset={offset}: {e}")
            break
        if not data.get('success'):
            print(f"[vtiger] Fallo: {data.get('error',{}).get('message','?')}")
            break
        records = data.get('result', [])
        all_records.extend(records)
        print(f"[vtiger] ... {len(all_records)} registros")
        if len(records) < page_size:
            break
        offset += page_size
    return all_records

_ID_CRUDO_RE = re.compile(r'^\d+x\d+$')
_account_name_cache = {}

def resolve_account_name(crmid):
    """La API de Vtiger a veces no resuelve el campo Organizacion (referencia a
    Cuenta) y devuelve el ID interno crudo (formato 'moduloxid') en vez del
    nombre. Esta funcion lo resuelve llamando al endpoint retrieve, con cache
    para no repetir llamadas cuando varias Producciones comparten cliente."""
    if crmid in _account_name_cache:
        return _account_name_cache[crmid]
    name = crmid  # fallback si la resolucion falla
    try:
        r = requests.get(f"{API_BASE}/retrieve", auth=_auth(),
                        params={'id': crmid}, timeout=30, verify=False)
        data = r.json()
        if data.get('success'):
            name = data.get('result', {}).get('accountname', '') or crmid
        else:
            print(f"[vtiger] No se pudo resolver cuenta {crmid}: {data.get('error')}")
    except Exception as e:
        print(f"[vtiger] Excepcion resolviendo cuenta {crmid}: {e}")
    _account_name_cache[crmid] = name
    return name

PROD = {
    'referencia': 'fld_vtcmproduccionname',
    'organizacion': 'cf_vtcmproduccion_organizacion',
    'codigo_siigo': 'cf_vtcmproduccion_coodigosiigo',
    'tipo': 'cf_vtcmproduccion_produccintipo',
    'fecha_prometida': 'cf_vtcmproduccion_fechaentregaprometida',
    'fecha_confirm_mp': 'cf_vtcmproduccion_fechaconfirmacindeentregademateriaprima',
    'etiquetas_rollo': 'cf_vtcmproduccion_nodeetiquetasporrollo',
    'fecha_liberacion': 'cf_vtcmproduccion_fecharealdeliberacin',
    'fecha_creacion': 'createdtime',
    'tiempo_prod': 'cf_vtcmproduccion_tiempoproduccin',
    'obs_ocop': 'cf_vtcmproduccion_observacionesocop',
    'estado': 'cf_vtcmproduccion_estado',
    'entrega_prod': 'cf_vtcmproduccion_entregadeproduccin',
    'asignado': 'assigned_user_id',
}

OP = {
    'referencia': 'fld_vtcmordendeproduccionname',
    'number': 'vtcmordendeproduccionnumber',
    'total_etiquetas': 'cf_vtcmordendeproduccion_totaletiquetas',
    'familia': 'cf_vtcmordendeproduccion_familia',
    'proceso': 'cf_vtcmordendeproduccion_procesodeetiquetas',
    'mts_lineales': 'cf_vtcmordendeproduccion_totalmtslineales',
    'fecha_entrega': 'cf_vtcmordendeproduccion_fechadeentrega',
    'material': 'cf_vtcmordendeproduccion_material',
    'adhesivo': 'cf_vtcmordendeproduccion_adhesivo',
    'ancho': 'cf_vtcmordendeproduccion_ancho',
    'centro_proceso': 'cf_vtcmordendeproduccion_centrodeproceso',
    'total_m2': 'cf_vtcmordendeproduccion_totalm2',
    'tipo_orden': 'cf_vtcmordendeproduccion_tipodeorden',
    'z': 'cf_vtcmordendeproduccion_z',
}

def _safe_num(val):
    try:
        if val is None or str(val).strip() == '': return 0
        return float(val)
    except: return 0

def _format_dt(v):
    if not v or str(v).strip() == '': return ''
    try:
        dt = datetime.strptime(str(v).strip()[:19], '%Y-%m-%d %H:%M:%S')
        return dt.strftime('%d-%m-%Y %I:%M %p')
    except: return str(v)

def fetch_and_process():
    if _SCIPY_OK:
        print("[sync] scipy disponible: emparejamiento Produccion-OP usara asignacion optima (Hungarian algorithm)")
    else:
        print("[sync] AVISO: scipy NO disponible -- emparejamiento Produccion-OP usara "
              "greedy (subóptimo). Agregar 'scipy' a requirements.txt para mejor precision.")
    # 1. Producciones con filtro fecha (01-01-2025 a 31-12-2026, segun informe original)
    print("[sync] Consultando producciones (fecha entre 2025-01-01 y 2026-12-31)...")
    producciones = vtiger_query(
        "SELECT * FROM vtcmproduccion WHERE cf_vtcmproduccion_fechaentregaprometida > '2024-12-31' "
        "AND cf_vtcmproduccion_fechaentregaprometida < '2027-01-01'"
    )
    print(f"[sync] Producciones brutas: {len(producciones)}")

    if not producciones:
        print("[sync] ERROR: Sin producciones")
        return False

    # === DIAGNOSTICO TEMPORAL: catalogar TODOS los valores unicos de entrega_prod ===
    _valores_vistos = {}
    for p in producciones:
        v = p.get(PROD['entrega_prod'])
        key = (repr(v), type(v).__name__)
        _valores_vistos[key] = _valores_vistos.get(key, 0) + 1
    print(f"[DEBUG-VALORES] Valores unicos de entrega_prod en las {len(producciones)} producciones brutas:")
    for (val_repr, tipo), n in sorted(_valores_vistos.items(), key=lambda x: -x[1]):
        print(f"[DEBUG-VALORES]   valor={val_repr} | tipo={tipo} | aparece {n} veces")

    # Filtro pre-match: solo por Asignado a Plataforma de Produccion (20x5).
    # El filtro de Entrega de Produccion (checkbox) se aplica DESPUES del
    # emparejamiento Produccion-OP para evitar contaminacion cruzada: cuando
    # una referencia tiene multiples reordenes, filtrar habilitadas ANTES del
    # match hacia que OPs de Producciones habilitadas fueran "robadas" por
    # Producciones no-habilitadas de la misma referencia.
    def _es_habilitado(val):
        return str(val).strip().lower() in ('1', 'true', 'habilitado', 'yes', 'si', 'on', 'verdadero')

    producciones = [p for p in producciones
                    if p.get(PROD['asignado'], '') == '20x5']
    print(f"[sync] Producciones asignadas a Plataforma (20x5): {len(producciones)} "
          f"(habilitadas AUN incluidas — se filtran DESPUES del emparejamiento)")

    # La API de Vtiger a veces no resuelve el campo Organizacion y devuelve el
    # ID interno crudo (ej. '3x6090') en vez del nombre de la cuenta. Se
    # detecta y resuelve aqui mismo, mutando el dict en su lugar para que todo
    # el resto del pipeline (agrupacion, filas, tambor, etc.) reciba siempre
    # el nombre ya resuelto sin cambios adicionales.
    _n_resueltos = 0
    for p in producciones:
        raw_org = str(p.get(PROD['organizacion'], ''))
        if _ID_CRUDO_RE.match(raw_org):
            p[PROD['organizacion']] = resolve_account_name(raw_org)
            _n_resueltos += 1
    if _n_resueltos:
        print(f"[sync] {_n_resueltos} Organizaciones resueltas manualmente "
              f"via retrieve ({len(_account_name_cache)} cuentas unicas consultadas)")

    # Indexar por REFERENCIA (nombre): agrupamos TODAS las producciones que comparten
    # una misma referencia (ej. reordenes del mismo diseno de etiqueta), sin descartar
    # ninguna. Cada grupo se ordena por fecha de creacion para poder emparejar 1 a 1.
    from collections import defaultdict
    prod_groups = defaultdict(list)
    for p in producciones:
        ref = p.get(PROD['referencia'], '').strip()
        if ref:
            prod_groups[ref].append(p)
    for ref in prod_groups:
        prod_groups[ref].sort(key=lambda p: str(p.get(PROD['fecha_creacion'], '')))
    print(f"[sync] Referencias unicas: {len(prod_groups)}")

    # 2. Ordenes: se filtra por su propia Fecha de Entrega (vuelve a ser el
    # campo canonico que gobierna todo el tablero). Igual que con Produccion,
    # solo el limite inferior funciona en la API ('>=' no esta soportado).
    print("[sync] Consultando ordenes (fecha de entrega > 2024-12-31)...")
    ordenes = vtiger_query(
        "SELECT * FROM vtcmordendeproduccion WHERE cf_vtcmordendeproduccion_fechadeentrega > '2024-12-31'"
    )
    print(f"[sync] Ordenes: {len(ordenes)}")

    if not ordenes:
        print("[sync] ERROR: Sin ordenes")
        return False

    # --- DEBUG TEMPORAL: buscar OP 104100 en la lista CRUDA, antes de cualquier
    # filtro/agrupacion, para saber si existe pero se pierde por proceso vacio
    # o por referencia con formato distinto (espacios, mayusculas, etc.) ---
    for op in ordenes:
        if '104100' in str(op.get(OP['number'], '')):
            print(f"[DEBUG-104100-CRUDO] number={repr(op.get(OP['number']))} | "
                  f"proceso={repr(op.get(OP['proceso'], ''))} | "
                  f"referencia={repr(op.get(OP['referencia'], ''))} | "
                  f"createdtime={op.get('createdtime', '')} | "
                  f"fecha_entrega={op.get(OP['fecha_entrega'], '')}")
    _ref_104100 = 'IC-TTT-70X32-R10000-M-1F-C30-MACHE-GENER-MAX-RESIST'
    _matches_prod = [k for k in prod_groups.keys() if k.strip() == _ref_104100.strip()]
    print(f"[DEBUG-104100-REF] Buscando referencia exacta {repr(_ref_104100)} en prod_groups: "
          f"{'ENCONTRADA' if _matches_prod else 'NO ENCONTRADA'} "
          f"({len(prod_groups)} referencias totales en prod_groups)")
    _similares = [k for k in prod_groups.keys() if 'MACHE-GENER-MAX-RESIST' in k]
    if _similares:
        for s in _similares:
            print(f"[DEBUG-104100-REF]   similar en prod_groups: {repr(s)}")

    # Agrupamos las OPs por referencia (sin ordenar por numero -- el emparejamiento
    # ahora se hace por cercania de fecha de creacion, ver mas abajo).
    def _norm_op(x):
        s = str(x).strip()
        return s[:-2] if s.endswith('.0') else s

    _raw_op_numbers = {_norm_op(op.get(OP['number'], '')) for op in ordenes}

    op_groups = defaultdict(list)
    for op in ordenes:
        proceso = op.get(OP['proceso'], '')
        if not proceso or proceso.strip() in ('', '-'):
            continue
        ref = op.get(OP['referencia'], '').strip()
        if ref:
            op_groups[ref].append(op)

    _op_groups_numbers = {_norm_op(op.get(OP['number'], ''))
                           for ops in op_groups.values() for op in ops}

    def _parse_any_dt(s):
        try:
            return datetime.strptime(str(s).strip()[:19], '%Y-%m-%d %H:%M:%S')
        except (ValueError, TypeError):
            return None

    # 3. Union 1 a 1: cada Produccion se empareja con UNA sola OP (relacion real es
    # 1:1, no muchos a muchos). Cuando una referencia se reordena muchas veces
    # (hasta 15+ veces para el mismo diseno de etiqueta, casos reales
    # observados), el emparejamiento debe minimizar la distancia TOTAL del
    # grupo completo, no solo la de cada par individual: un algoritmo greedy
    # que toma el par mas cercano primero puede "robar" una OP casi perfecta
    # para una Produccion vecina, empeorando la asignacion global.
    #
    # Caso real que expuso esto: referencia con 13 Producciones y 13 OPs
    # (proporcion 1:1 exacta). Greedy-por-par-mas-cercano-primero asigno
    # NP107865 (la mas nueva, con match casi perfecto de 2h) a la OP 104504,
    # dejando a NP107833 (la correcta segun Vtiger) forzada a emparejar con
    # 104526 a 27h de distancia -- desajuste total del grupo: 29h.
    # La asignacion OPTIMA (Hungarian algorithm) da NP107833<->104504 (20h) +
    # NP107865<->104526 (4h) = 24h de desajuste total -- MENOR, y coincide
    # exactamente con la relacion real confirmada en Vtiger.
    #
    # Fix: usar scipy.optimize.linear_sum_assignment (Hungarian algorithm)
    # para encontrar la asignacion de MINIMA DISTANCIA TOTAL por referencia,
    # en vez de greedy por par mas cercano primero. Si scipy no esta
    # disponible en el entorno (ej. falta en requirements.txt), cae de
    # vuelta al greedy anterior (funcional pero subóptimo) sin romper el sync.
    rows = []
    sin_op = 0
    _n_ajustados = 0
    _n_optimo = 0
    _n_fallback = 0
    for ref, prods in prod_groups.items():
        ops_disponibles = list(op_groups.get(ref, []))
        if len(ops_disponibles) > len(prods):
            _n_ajustados += 1
        if not ops_disponibles:
            sin_op += len(prods)
            continue

        n_prod = len(prods)
        n_op = len(ops_disponibles)
        asignacion = {}  # pi -> oi
        _debug_esta_ref = (ref.strip() == 'BL-TTT-22X76-R8000-M-4F-C30-PINTUCO')
        if _debug_esta_ref:
            print(f"[DEBUG-INLINE] ref={repr(ref)} | n_prod={n_prod} | n_op={n_op} | "
                  f"scipy_ok={_SCIPY_OK}")
            print(f"[DEBUG-INLINE] prods NP: {[p.get('vtcmproduccionnumber') for p in prods]}")
            print(f"[DEBUG-INLINE] ops number: {[o.get(OP['number']) for o in ops_disponibles]}")

        if _SCIPY_OK:
            _n_optimo += 1
            _SIN_FECHA = 1e15  # costo centinela para pares sin fecha valida (no se asignan)
            cost = np.full((n_prod, n_op), _SIN_FECHA)
            for pi, prod in enumerate(prods):
                prod_dt = _parse_any_dt(prod.get(PROD['fecha_creacion'], ''))
                if prod_dt is None:
                    continue
                for oi, op in enumerate(ops_disponibles):
                    op_dt = _parse_any_dt(op.get('createdtime', ''))
                    if op_dt is not None:
                        cost[pi, oi] = abs((op_dt - prod_dt).total_seconds())
            row_ind, col_ind = linear_sum_assignment(cost)
            for pi, oi in zip(row_ind, col_ind):
                if cost[pi, oi] < _SIN_FECHA:
                    asignacion[pi] = oi
            if _debug_esta_ref:
                print(f"[DEBUG-INLINE] cost matrix (horas):")
                for pi in range(n_prod):
                    fila = [round(cost[pi,oi]/3600,1) if cost[pi,oi] < _SIN_FECHA else -1 for oi in range(n_op)]
                    print(f"[DEBUG-INLINE]   NP={prods[pi].get('vtcmproduccionnumber')}: {fila}")
                print(f"[DEBUG-INLINE] row_ind={list(row_ind)} col_ind={list(col_ind)}")
                for pi, oi in asignacion.items():
                    print(f"[DEBUG-INLINE] asignacion: NP={prods[pi].get('vtcmproduccionnumber')} "
                          f"<-> OP={ops_disponibles[oi].get(OP['number'])} "
                          f"(costo={cost[pi,oi]/3600:.1f}h)")
        else:
            _n_fallback += 1
            # Fallback greedy (subóptimo, ver comentario arriba)
            pares = []
            for pi, prod in enumerate(prods):
                prod_dt = _parse_any_dt(prod.get(PROD['fecha_creacion'], ''))
                for oi, op in enumerate(ops_disponibles):
                    op_dt = _parse_any_dt(op.get('createdtime', ''))
                    if prod_dt is not None and op_dt is not None:
                        dist = abs((op_dt - prod_dt).total_seconds())
                    else:
                        dist = float('inf')
                    pares.append((dist, pi, oi))
            pares.sort(key=lambda x: x[0])
            prod_usada = [False] * n_prod
            op_usada = [False] * n_op
            for dist, pi, oi in pares:
                if prod_usada[pi] or op_usada[oi]:
                    continue
                prod_usada[pi] = True
                op_usada[oi] = True
                asignacion[pi] = oi

        for pi, prod in enumerate(prods):
            if pi not in asignacion:
                sin_op += 1
                continue
            op = ops_disponibles[asignacion[pi]]
            proceso = op.get(OP['proceso'], '')

            rows.append({
                'fecha_entrega_raw': op.get(OP['fecha_entrega'], ''),
                'fecha_creacion_raw': _format_dt(prod.get(PROD['fecha_creacion'], '')),
                'maquina_raw': op.get(OP['centro_proceso'], ''),
                'etapa': proceso,
                'organizacion': prod.get(PROD['organizacion'], ''),
                'referencia': prod.get(PROD['referencia'], ''),
                'codigo_siigo': prod.get(PROD['codigo_siigo'], ''),
                'op_number': op.get(OP['number'], ''),
                'total_etiquetas': _safe_num(op.get(OP['total_etiquetas'], 0)),
                'tipo': prod.get(PROD['tipo'], ''),
                'familia': op.get(OP['familia'], ''),
                'mts_lineales': _safe_num(op.get(OP['mts_lineales'], 0)),
                'fecha_prometida': prod.get(PROD['fecha_prometida'], ''),
                'fecha_confirm_mp': prod.get(PROD['fecha_confirm_mp'], ''),
                'material': op.get(OP['material'], ''),
                'adhesivo': op.get(OP['adhesivo'], ''),
                'ancho': _safe_num(op.get(OP['ancho'], 0)),
                'etiquetas_rollo': _safe_num(prod.get(PROD['etiquetas_rollo'], 0)),
                'fecha_liberacion': prod.get(PROD['fecha_liberacion'], ''),
                'total_m2': _safe_num(op.get(OP['total_m2'], 0)),
                'tipo_orden': op.get(OP['tipo_orden'], ''),
                'tiempo_prod': prod.get(PROD['tiempo_prod'], ''),
                'z': op.get(OP['z'], ''),
                'obs_ocop': prod.get(PROD['obs_ocop'], ''),
                'entrega_prod_raw': prod.get(PROD['entrega_prod'], ''),
                'prod_createdtime': prod.get(PROD['fecha_creacion'], ''),
                'op_createdtime': op.get('createdtime', ''),
                'prod_id': prod.get('id'),
                'op_id': op.get('id'),
            })

    print(f"[sync] {sin_op} Producciones sin OP, {len(rows)} filas ANTES de filtro habilitadas "
          f"({_n_ajustados} referencias con reordenes historicos ajustadas)")
    print(f"[sync] Emparejamiento: {_n_optimo} referencias con asignacion optima, "
          f"{_n_fallback} con greedy fallback")
    if not rows:
        print("[sync] ERROR: 0 filas construidas")
        return False

    _matched_numbers = {_norm_op(r.get('op_number', '')) for r in rows}
    # Snapshot ANTES de cualquier filtro: se usa para construir el dataset de
    # Tambor General (y demas pestanas operativas), que solo debe respetar
    # Asignado a Plataforma + Fecha Entrega Prometida en rango + Entrega de
    # Produccion != Habilitado -- SIN los filtros extra de proximidad/seguridad
    # (esos son exclusivos de la alerta de Incumplidas, ver 'df' mas abajo).
    _rows_all_matched = list(rows)

    # --- FILTRO 1: excluir habilitadas (post-match) ---
    rows_pre = len(rows)
    rows = [r for r in rows if not _es_habilitado(r.get('entrega_prod_raw', ''))]
    print(f"[sync] Filas tras excluir habilitadas: {len(rows)} "
          f"(excluidas {rows_pre - len(rows)} habilitadas)")
    _post_f1_numbers = {_norm_op(r.get('op_number', '')) for r in rows}

    # --- FILTRO 2: proximidad de fechas de creacion ---
    # Si la Produccion y la OP fueron creadas a mas de MAX_DIAS dias de
    # diferencia, es muy probable un emparejamiento erroneo (la OP pertenece
    # a otra Produccion de la misma referencia, de una reorden distinta).
    _MAX_DIAS = 2
    rows_pre2 = len(rows)
    rows_ok = []
    for r in rows:
        prod_dt = _parse_any_dt(r.get('prod_createdtime', ''))
        op_dt = _parse_any_dt(r.get('op_createdtime', ''))
        if prod_dt and op_dt:
            diff = abs((op_dt - prod_dt).days)
            if diff > _MAX_DIAS:
                print(f"[sync] Excluida OP {r['op_number']} por proximidad: "
                      f"Prod={r['prod_createdtime'][:10]} OP={r['op_createdtime'][:10]} "
                      f"diff={diff}d >{_MAX_DIAS}d | ref={r['referencia']}")
                continue
        rows_ok.append(r)
    rows = rows_ok
    if rows_pre2 != len(rows):
        print(f"[sync] Filas tras filtro proximidad: {len(rows)} "
              f"(excluidas {rows_pre2 - len(rows)} por diff >{_MAX_DIAS} dias)")
    _post_f2_numbers = {_norm_op(r.get('op_number', '')) for r in rows}

    # --- FILTRO 3 (SEGURIDAD): excluir si CUALQUIER Produccion de la misma
    # referencia -- no solo la que quedo emparejada -- fue creada muy cerca
    # en el tiempo de esta OP y esta habilitada -- PERO solo si esa Produccion
    # habilitada compite genuinamente por esta OP (esta tan cerca o mas cerca
    # que la Produccion con la que realmente quedo emparejada). Si el
    # emparejamiento ya es claramente el mas cercano posible, se confia en el
    # y NO se excluye -- version anterior (solo "existe alguna habilitada
    # cerca") generaba falsos negativos: ocultaba emparejamientos correctos
    # solo porque una Produccion habilitada de la misma referencia, MAS LEJANA
    # en el tiempo que la real, tambien caia dentro de la ventana de dias.
    # Caso real que corrigio esto: OP 104571 <-> NP107946 (match correcto,
    # 30 min de diferencia) se ocultaba porque otra Produccion habilitada de
    # la misma referencia, mas lejana, tambien estaba dentro de +-2 dias.
    _MAX_DIAS_HAB = 2
    _MARGEN_SEGURIDAD_SEG = 12 * 3600  # 12h de margen para considerar "competencia real"
    _hab_por_ref = defaultdict(list)
    for p in producciones:
        if _es_habilitado(p.get(PROD['entrega_prod'], '')):
            ref_p = p.get(PROD['referencia'], '').strip()
            dt_p = _parse_any_dt(p.get(PROD['fecha_creacion'], ''))
            if ref_p and dt_p:
                _hab_por_ref[ref_p].append(dt_p)

    rows_pre3 = len(rows)
    rows_ok = []
    for r in rows:
        op_dt = _parse_any_dt(r.get('op_createdtime', ''))
        prod_dt = _parse_any_dt(r.get('prod_createdtime', ''))
        ref = r['referencia']
        riesgo = False
        candidata_riesgo = None
        if op_dt and ref in _hab_por_ref:
            if prod_dt:
                # Distancia real del emparejamiento actual: solo se excluye si
                # existe una habilitada tan cerca o mas cerca (+margen) que ella.
                d_matched = abs((prod_dt - op_dt).total_seconds())
                for hab_dt in _hab_por_ref[ref]:
                    d_hab = abs((hab_dt - op_dt).total_seconds())
                    if d_hab <= d_matched + _MARGEN_SEGURIDAD_SEG:
                        riesgo = True
                        candidata_riesgo = hab_dt
                        break
            else:
                # Sin fecha de creacion de la Produccion emparejada: no se
                # puede comparar distancias, se mantiene el comportamiento
                # conservador original (ventana absoluta de dias).
                for hab_dt in _hab_por_ref[ref]:
                    if abs((hab_dt - op_dt).days) <= _MAX_DIAS_HAB:
                        riesgo = True
                        candidata_riesgo = hab_dt
                        break
        if riesgo:
            print(f"[sync] Excluida OP {r['op_number']} por SEGURIDAD: Produccion "
                  f"habilitada de la misma referencia compite genuinamente "
                  f"(creada {candidata_riesgo}) | ref={ref}")
            continue
        rows_ok.append(r)
    rows = rows_ok
    if rows_pre3 != len(rows):
        print(f"[sync] Filas tras filtro SEGURIDAD habilitadas cercanas: {len(rows)} "
              f"(excluidas {rows_pre3 - len(rows)})")
    _post_f3_numbers = {_norm_op(r.get('op_number', '')) for r in rows}

    # --- DEBUG TEMPORAL: diagnostico contra reporte de Vtiger (250 OPs esperadas
    # con criterio Asignado a Plataforma + Fecha Entrega Prometida 2025-2026 +
    # Entrega de Produccion != Habilitado). Para cada OP del reporte que no
    # sobrevivio hasta el resultado final, se indica en que etapa se perdio.
    _VTIGER_250 = {
        '101366', '101372', '103460', '103480', '103481', '103524', '103548', '103550', '103860', '103861',
        '103883', '104095', '104097', '104098', '104210', '104223', '104264', '104306', '104307', '104308',
        '104310', '104311', '104324', '104326', '104346', '104349', '104350', '104352', '104353', '104354',
        '104393', '104394', '104431', '104436', '104439', '104440', '104441', '104449', '104450', '104478',
        '104486', '104487', '104488', '104489', '104490', '104491', '104492', '104509', '104510', '104516',
        '104575', '104576', '104578', '104592', '104594', '104605', '104608', '104619', '104621', '104622',
        '104623', '104637', '104638', '104639', '104648', '104650', '104657', '104658', '104660', '104661',
        '104663', '104666', '104668', '104670', '104679', '104681', '104683', '104685', '104686', '104687',
        '104689', '104690', '104691', '104692', '104693', '104695', '104696', '104699', '104700', '104701',
        '104711', '104712', '104713', '104714', '104715', '104716', '104719', '104721', '104726', '104734',
        '104735', '104736', '104737', '104738', '104739', '104740', '104743', '104744', '104745', '104746',
        '104747', '104748', '104749', '104750', '104751', '104752', '104753', '104754', '104755', '104756',
        '104757', '104758', '104759', '104760', '104762', '104763', '104764', '104766', '104767', '104768',
        '104769', '104771', '104774', '104775', '104776', '104777', '104778', '104779', '104780', '104781',
        '104782', '104783', '104784', '104785', '104790', '104791', '104792', '104793', '104794', '104795',
        '104796', '104797', '104798', '104799', '104800', '104801', '104802', '104803', '104804', '104805',
        '104806', '104807', '104808', '104809', '104810', '104811', '104812', '104813', '104814', '104815',
        '104816', '104817', '104818', '104820', '104821', '104822', '104823', '104824', '104825', '104826',
        '104827', '104828', '104829', '104830', '104831', '104832', '104835', '104836', '104837', '104838',
        '104839', '104840', '104841', '104842', '104843', '104844', '104845', '104846', '104847', '104848',
        '104849', '104850', '104851', '104852', '104853', '104854', '104855', '104856', '104857', '104858',
        '104859', '104860', '104861', '104862', '104863', '104864', '104865', '104866', '104867', '104868',
        '104869', '104870', '104871', '104872', '104873', '104874', '104875', '104876', '104877', '104878',
        '104879', '104880', '104881', '104882', '104883', '104884', '104885', '104886', '104887', '104888',
        '104889', '104890', '104891', '104892', '104893', '104894', '104895', '104896', '104897', '104898',
        '104899', '104900', '104901', '104902', '104903', '104904', '104905', '104906', '104907', '104908',
        '104909', '104910', '104911', '104912', '104913', '104914', '104915', '104916', '104917', '104918',
        '104919', '104920', '104921', '104922', '104923', '104924', '104925', '104926', '104927', '104928',
        '104929', '104930', '104931', '104932', '104933', '104934', '104935', '104936', '104937', '104938',
        '104939', '104940', '104941', '104942', '104943', '104944', '104945', '104946', '104947', '104948',
        '104950', '104951', '104952', '104953', '104954', '104955', '104956', '104957', '104958', '104959',
        '104960', '104961', '104962', '104963', '104964', '104965', '104966', '104967', '104968', '104969',
    }
    _faltantes = sorted(_VTIGER_250 - _post_f3_numbers)
    print(f"[sync] DIAGNOSTICO vs Vtiger: {len(_VTIGER_250)} esperadas, "
          f"{len(_VTIGER_250 & _post_f3_numbers)} presentes, {len(_faltantes)} faltantes")
    for op_n in _faltantes:
        if op_n not in _raw_op_numbers:
            razon = "NO existe en la respuesta de la API de Ordenes (fuera del filtro fechadeentrega>2024-12-31, o dato distinto)"
        elif op_n not in _op_groups_numbers:
            razon = "excluida por campo Proceso de Etiquetas vacio o '-'"
        elif op_n not in _matched_numbers:
            razon = "no fue seleccionada en el emparejamiento 1:1 (otra OP de la misma referencia gano la asignacion por cercania de fecha)"
        elif op_n not in _post_f1_numbers:
            razon = "excluida por FILTRO 1 (la Produccion emparejada esta habilitada)"
        elif op_n not in _post_f2_numbers:
            razon = "excluida por FILTRO 2 (proximidad de creacion Produccion/OP >2 dias)"
        elif op_n not in _post_f3_numbers:
            razon = "excluida por FILTRO 3 (seguridad: otra Produccion habilitada de la misma referencia cerca en el tiempo)"
        else:
            razon = "presente en resultado final -- revisar filtro adicional en construccion de Tambor General"
        print(f"[DEBUG-FALTANTE] OP {op_n}: {razon}")

    # --- DEBUG TEMPORAL: caso puntual OP 104504 / NP107833 (reportado con
    # casilla Entrega de Produccion en TRUE pero sigue en Incumplidas) ---
    _ref_caso_completa = 'BL-TTT-22X76-R8000-M-4F-C30-PINTUCO'
    _prods_ref_caso = [p for p in producciones if p.get(PROD['referencia'], '').strip() == _ref_caso_completa]
    print(f"[DEBUG-CASOREF-PROD] '{_ref_caso_completa}': {len(_prods_ref_caso)} Producciones:")
    for p in sorted(_prods_ref_caso, key=lambda x: str(x.get(PROD['fecha_creacion'], ''))):
        print(f"[DEBUG-CASOREF-PROD]   NUMERO={p.get('vtcmproduccionnumber')} | id={p.get('id')} | "
              f"created={p.get(PROD['fecha_creacion'],'')} | "
              f"fecha_prometida={p.get(PROD['fecha_prometida'],'')} | "
              f"entrega_prod={repr(p.get(PROD['entrega_prod'],''))} | "
              f"hab={_es_habilitado(p.get(PROD['entrega_prod'],''))}")
    _ops_ref_caso = op_groups.get(_ref_caso_completa, [])
    print(f"[DEBUG-CASOREF-OP] '{_ref_caso_completa}': {len(_ops_ref_caso)} OPs disponibles:")
    for o in sorted(_ops_ref_caso, key=lambda x: str(x.get('createdtime', ''))):
        print(f"[DEBUG-CASOREF-OP]   id={o.get('id')} | number={o.get(OP['number'])} | "
              f"created={o.get('createdtime','')} | "
              f"fecha_entrega={o.get(OP['fecha_entrega'],'')}")

    _op_caso = '104504'
    _np_caso = 'NP107833'
    for op in ordenes:
        if _norm_op(op.get(OP['number'], '')) == _op_caso:
            print(f"[DEBUG-CASO-OP] {_op_caso} CRUDO | referencia={repr(op.get(OP['referencia'], ''))} | "
                  f"proceso={repr(op.get(OP['proceso'], ''))} | "
                  f"createdtime={op.get('createdtime', '')} | "
                  f"fecha_entrega={op.get(OP['fecha_entrega'], '')}")
    for p in producciones:
        if p.get('vtcmproduccionnumber') == _np_caso:
            print(f"[DEBUG-CASO-NP] {_np_caso} CRUDO | id={p.get('id')} | "
                  f"referencia={repr(p.get(PROD['referencia'], ''))} | "
                  f"entrega_prod={repr(p.get(PROD['entrega_prod'], ''))} | "
                  f"hab={_es_habilitado(p.get(PROD['entrega_prod'], ''))} | "
                  f"asignado={p.get(PROD['asignado'])} | "
                  f"created={p.get(PROD['fecha_creacion'], '')} | "
                  f"fecha_prometida={p.get(PROD['fecha_prometida'], '')}")
    # Con que Produccion quedo emparejada esta OP (si llego a emparejarse)
    for r in _rows_all_matched:
        if _norm_op(r.get('op_number', '')) == _op_caso:
            print(f"[DEBUG-CASO-MATCH] OP {_op_caso} emparejada con prod_id={r.get('prod_id')} | "
                  f"entrega_prod={repr(r.get('entrega_prod_raw'))} | "
                  f"hab={_es_habilitado(r.get('entrega_prod_raw'))} | "
                  f"prod_created={r.get('prod_createdtime')} | op_created={r.get('op_createdtime')} | "
                  f"referencia={r.get('referencia')}")
    if _op_caso not in _raw_op_numbers:
        print(f"[DEBUG-CASO] {_op_caso}: NO existe en la respuesta cruda de Ordenes")
    elif _op_caso not in _op_groups_numbers:
        print(f"[DEBUG-CASO] {_op_caso}: excluida por Proceso de Etiquetas vacio o '-'")
    elif _op_caso not in _matched_numbers:
        print(f"[DEBUG-CASO] {_op_caso}: no fue seleccionada en el emparejamiento 1:1")
    elif _op_caso not in _post_f1_numbers:
        print(f"[DEBUG-CASO] {_op_caso}: excluida por FILTRO 1 (produccion emparejada habilitada)")
    elif _op_caso not in _post_f2_numbers:
        print(f"[DEBUG-CASO] {_op_caso}: excluida por FILTRO 2 (proximidad >2 dias)")
    elif _op_caso not in _post_f3_numbers:
        print(f"[DEBUG-CASO] {_op_caso}: excluida por FILTRO 3 (seguridad: habilitada cercana)")
    else:
        _hoy_bogota = datetime.now(BOGOTA).date()
        _fe_caso = next((r.get('fecha_entrega_raw') for r in rows if _norm_op(r.get('op_number','')) == _op_caso), None)
        print(f"[DEBUG-CASO] {_op_caso}: SOBREVIVIO los 3 filtros y esta en 'df' | "
              f"fecha_entrega_raw={_fe_caso} | hoy_bogota={_hoy_bogota} | "
              f"revisar si fecha_entrega < hoy para que cuente como vencida")

    # 4. Procesar
    df = pd.DataFrame(rows)
    # 'fecha_entrega' es la fuente unica de verdad para TODO el tablero (panorama
    # semanal, colores TOC, capacidad RRC, cuellos de botella, tambor, diagnostico
    # e incumplidas): se toma de Fecha de Entrega (modulo Orden de Produccion),
    # que es la fecha operativa real de planta. Fecha Entrega Prometida (modulo
    # Produccion) se guarda en 'fecha_prometida' para referencia pero ya no
    # gobierna el tablero.
    df['fecha_entrega'] = pd.to_datetime(df['fecha_entrega_raw'], errors='coerce')
    df['fecha_creacion'] = pd.to_datetime(df['fecha_creacion_raw'], format='%d-%m-%Y %I:%M %p', errors='coerce')
    df['mts'] = df['mts_lineales']
    # Asignacion de maquina: usa etapa (rebobinado/troquelado) + primera maquina
    # listada en Centro de Proceso como fallback (separador '|##|' de la API)
    df['maquina'] = df.apply(asignar_maquina, axis=1)

    # --- Dataset PARALELO exclusivo para Tambor General: NO usa los filtros de
    # proximidad (2) ni seguridad (3), que son intencionalmente conservadores
    # para la alerta de Incumplidas. Tambor General debe reflejar exactamente
    # el criterio de negocio: Asignado a Plataforma + Fecha Entrega Prometida
    # en rango + Entrega de Produccion != Habilitado -- sin filtrar por si esta
    # vencida o no, y sin las heuristicas extra de seguridad del emparejamiento.
    rows_tambor = [r for r in _rows_all_matched if not _es_habilitado(r.get('entrega_prod_raw', ''))]
    df_tambor = pd.DataFrame(rows_tambor)
    df_tambor['fecha_entrega'] = pd.to_datetime(df_tambor['fecha_entrega_raw'], errors='coerce')
    df_tambor['fecha_creacion'] = pd.to_datetime(df_tambor['fecha_creacion_raw'], format='%d-%m-%Y %I:%M %p', errors='coerce')
    df_tambor['mts'] = df_tambor['mts_lineales']
    df_tambor['maquina'] = df_tambor.apply(asignar_maquina, axis=1)
    print(f"[sync] df_tambor (Tambor General, solo Filtro 1): {len(df_tambor)} filas "
          f"(vs {len(df)} en df principal con los 3 filtros)")

    # --- DEBUG TEMPORAL: diagnostico de "Dias Ofrecidos por Tipo de Etiqueta"
    # en blanco -- verificar si fecha_creacion/fecha_entrega se parsean bien
    # en df_tambor y si dias_ofrecidos resulta > 0 para las filas
    _nat_fc_df = df['fecha_creacion'].isna().sum()
    _nat_fe_df = df['fecha_entrega'].isna().sum()
    _nat_fc_tb = df_tambor['fecha_creacion'].isna().sum()
    _nat_fe_tb = df_tambor['fecha_entrega'].isna().sum()
    print(f"[DEBUG-TAMBOR-FECHAS] df: fecha_creacion NaT={_nat_fc_df}/{len(df)} | "
          f"fecha_entrega NaT={_nat_fe_df}/{len(df)}")
    print(f"[DEBUG-TAMBOR-FECHAS] df_tambor: fecha_creacion NaT={_nat_fc_tb}/{len(df_tambor)} | "
          f"fecha_entrega NaT={_nat_fe_tb}/{len(df_tambor)}")
    _dias_tb = (df_tambor['fecha_entrega'].dt.normalize() - df_tambor['fecha_creacion'].dt.normalize()).dt.days
    _dias_positivos = (_dias_tb > 0).sum()
    print(f"[DEBUG-TAMBOR-FECHAS] df_tambor: filas con dias_ofrecidos>0: "
          f"{_dias_positivos}/{len(df_tambor)}")
    for i, r in enumerate(rows_tambor[:8]):
        print(f"[DEBUG-TAMBOR-SAMPLE] op={r.get('op_number')} | "
              f"fecha_creacion_raw={repr(r.get('fecha_creacion_raw'))} | "
              f"fecha_entrega_raw={repr(r.get('fecha_entrega_raw'))} | "
              f"tiempo_prod={repr(r.get('tiempo_prod'))}")

    _tambor_numbers = {_norm_op(r.get('op_number', '')) for r in rows_tambor}
    _tambor_faltantes = sorted(_VTIGER_250 - _tambor_numbers)
    print(f"[sync] DIAGNOSTICO Tambor vs Vtiger: {len(_VTIGER_250)} esperadas, "
          f"{len(_VTIGER_250 & _tambor_numbers)} presentes, {len(_tambor_faltantes)} faltantes")
    if _tambor_faltantes:
        print(f"[sync] Aun faltan en Tambor: {_tambor_faltantes}")

    # --- DEBUG TEMPORAL: traza de las OPs reportadas en Tambor General ---
    # "Deberian estar" (con su NP correcta segun Vtiger, confirmada por el
    # usuario) y "No deberian estar" (actualmente mal, sin NP confirmada).
    _casos_deberian_estar = {
        '103460': 'NP106568', '103548': 'NP106652', '103524': 'NP106655',
        '104098': 'NP107360', '104095': 'NP107362', '104431': 'NP107734',
    }
    _casos_no_deberian_estar = {'103461', '104100', '103525', '103549', '104096', '104434'}
    _todos_casos = set(_casos_deberian_estar.keys()) | _casos_no_deberian_estar

    for op_c in sorted(_todos_casos):
        etiqueta = f"DEBERIA ({_casos_deberian_estar[op_c]})" if op_c in _casos_deberian_estar else "NO DEBERIA"
        match_row = next((r for r in _rows_all_matched if _norm_op(r.get('op_number', '')) == op_c), None)
        en_tambor = op_c in _tambor_numbers
        if match_row:
            prod_id = match_row.get('prod_id')
            np_match = next((p.get('vtcmproduccionnumber') for p in producciones if p.get('id') == prod_id), '?')
            print(f"[DEBUG-TAMBORCASO] OP {op_c} [{etiqueta}]: emparejada con {np_match} (id={prod_id}) | "
                  f"entrega_prod={repr(match_row.get('entrega_prod_raw'))} | "
                  f"hab={_es_habilitado(match_row.get('entrega_prod_raw'))} | "
                  f"prod_created={match_row.get('prod_createdtime')} | op_created={match_row.get('op_createdtime')} | "
                  f"referencia={match_row.get('referencia')} | EN_TAMBOR={en_tambor}")
        else:
            razon = ("NO existe en Ordenes" if op_c not in _raw_op_numbers else
                      "excluida por Proceso vacio" if op_c not in _op_groups_numbers else
                      "no emparejada 1:1")
            print(f"[DEBUG-TAMBORCASO] OP {op_c} [{etiqueta}]: SIN match -- {razon} | EN_TAMBOR={en_tambor}")

    # Volcado de la NP esperada (para las 6 que "deberian estar") con su
    # estado real: confirmar si esta habilitada o no
    for op_c, np_esperada in _casos_deberian_estar.items():
        for p in producciones:
            if p.get('vtcmproduccionnumber') == np_esperada:
                print(f"[DEBUG-TAMBORCASO-NP] {np_esperada} (esperada para OP {op_c}) | id={p.get('id')} | "
                      f"referencia={repr(p.get(PROD['referencia'], ''))} | "
                      f"entrega_prod={repr(p.get(PROD['entrega_prod'], ''))} | "
                      f"hab={_es_habilitado(p.get(PROD['entrega_prod'], ''))} | "
                      f"created={p.get(PROD['fecha_creacion'], '')}")

    # --- DEBUG TEMPORAL: volcado completo de las 6 referencias problematicas
    # con campos candidatos de desempate cuando Produccion/OP se crean casi
    # simultaneamente (mismo lote/batch, diferencia de segundos) y la cercania
    # de tiempo ya no alcanza para distinguir cual va con cual.
    _refs_batch = {
        'IC-PPT-88X120-R2000-M-1F-C30-ELEMENTZ-3N1-COCONUT-HAIR-BODY-WASH-443ML',
        'BL-PPB-110X80-R2000-S-1F-C30-CORTE-ESPECIAL-SUPERF',
        'IC-ESM-95X220-R1500-S-1F-C30-ESEN-FLEISCH-VAINILLA-OSCURA-500ml-COLOMBIA',
        'IC-TTT-70X42-R5000-M-1F-C30-ETQ-CUCHILLA-BELLOTA',
        'IC-TTT-70X32-R10000-M-1F-C30-MACHE-GENER-MAX-RESIST',
        'IC-P60-100X245-R500-P-1F-C15-ETQ-LAV-LIQ-ANTBAC-LIM-ALKS-X-3,7L',
    }
    for ref_b in sorted(_refs_batch):
        prods_b = [p for p in producciones if p.get(PROD['referencia'], '').strip() == ref_b]
        # Solo mostrar las mas recientes (2026) para no saturar el log
        prods_b = [p for p in prods_b if str(p.get(PROD['fecha_creacion'], '')).startswith('2026')]
        print(f"[DEBUG-BATCH-PROD] '{ref_b}': {len(prods_b)} Producciones 2026:")
        for p in sorted(prods_b, key=lambda x: str(x.get(PROD['fecha_creacion'], ''))):
            print(f"[DEBUG-BATCH-PROD]   NUMERO={p.get('vtcmproduccionnumber')} | id={p.get('id')} | "
                  f"created={p.get(PROD['fecha_creacion'],'')} | "
                  f"entrega_prod={repr(p.get(PROD['entrega_prod'],''))} | "
                  f"fecha_prometida={p.get(PROD['fecha_prometida'],'')} | "
                  f"cantidad={p.get('cf_vtcmproduccion_cantidad','')} | "
                  f"repeticiones={p.get('cf_vtcmproduccion_repeticionesproduccin','')}")
        ops_b = op_groups.get(ref_b, [])
        ops_b = [o for o in ops_b if str(o.get('createdtime', '')).startswith('2026')]
        print(f"[DEBUG-BATCH-OP] '{ref_b}': {len(ops_b)} OPs 2026:")
        for o in sorted(ops_b, key=lambda x: str(x.get('createdtime', ''))):
            print(f"[DEBUG-BATCH-OP]   number={o.get(OP['number'])} | id={o.get('id')} | "
                  f"created={o.get('createdtime','')} | "
                  f"fecha_entrega={o.get(OP['fecha_entrega'],'')} | "
                  f"total_etiquetas={o.get(OP['total_etiquetas'],'')} | "
                  f"repeticiones={o.get('cf_vtcmordendeproduccion_repeticiones','')}")


    now_bogota = datetime.now(BOGOTA)
    today = pd.Timestamp(now_bogota.date())
    days_until_monday = (7 - today.weekday()) % 7
    if days_until_monday == 0: days_until_monday = 7
    lun = today + pd.Timedelta(days=days_until_monday)
    if today.weekday() < 5:
        lun = today - pd.Timedelta(days=today.weekday())
    vie = lun + pd.Timedelta(days=4)
    incumplidas = df[df['fecha_entrega'] < today]

    # Agrupar incumplidas por etapa (estatus) con clientes y tipo
    # (tipo se deriva de la referencia, igual que process_excel, para consistencia
    # entre el flujo manual y el automatico)
    inc_etapas_raw = {}
    for etapa, grupo in incumplidas.groupby('etapa'):
        inc_etapas_raw[etapa] = {
            'total': len(grupo),
            'ordenes': [
                {'cliente': row['organizacion'], 'tipo': get_tipo(row['referencia']), 'op': row['op_number']}
                for _, row in grupo.iterrows()
            ]
        }
    inc_etapas = dict(sorted(inc_etapas_raw.items(), key=lambda x: x[1]['total'], reverse=True))

    etapas_unicas = sorted(incumplidas['etapa'].dropna().unique().tolist())

    # El campo mostrado como 'fecha_entrega' en el detalle de incumplidas es la
    # Fecha Entrega Prometida (Produccion) - la fecha que realmente se incumplio
    inc_detalle = sorted([
        {
            'cliente': row['organizacion'],
            'referencia': row['referencia'],
            'op': row['op_number'],
            'tipo': get_tipo(row['referencia']),
            'etapa': row['etapa'],
            'fecha_entrega': row['fecha_entrega'].strftime('%d/%m/%Y') if pd.notna(row['fecha_entrega']) else '',
        }
        for _, row in incumplidas.iterrows()
    ], key=lambda x: x['fecha_entrega'])

    maquinas = sorted(df['maquina'].dropna().unique().tolist())
    dias = pd.date_range(lun, vie, freq='B')
    dias_str = [d.strftime('%Y-%m-%d') for d in dias]
    dias_label = [d.strftime('%d/%m') for d in dias]
    dias_nombre = ['Lunes', 'Martes', 'Miercoles', 'Jueves', 'Viernes']

    pivot, totales_dia = {}, {d: 0 for d in dias_str}
    for m in maquinas:
        pivot[m] = {}
        for d in dias_str:
            n = len(df[(df['maquina'] == m) & (df['fecha_entrega'].dt.strftime('%Y-%m-%d') == d)])
            pivot[m][d] = n
            totales_dia[d] += n

    dia_max_nombre, dia_max_val, dia_max_fecha = '', 0, ''
    total_semana = sum(totales_dia.values())
    inc_maq = incumplidas['maquina'].value_counts().to_dict()
    if totales_dia:
        dia_max_key = max(totales_dia, key=totales_dia.get)
        dia_max_idx = dias_str.index(dia_max_key) if dia_max_key in dias_str else 0
        dia_max_nombre = dias_nombre[dia_max_idx] if dia_max_idx < len(dias_nombre) else ''
        dia_max_val = totales_dia[dia_max_key]
        dia_max_fecha = dias_label[dia_max_idx] if dia_max_idx < len(dias_label) else ''

    # ==================== LOGICA TOC (Teoria de Restricciones) ====================
    # Portada desde process_excel() para que /dia, /tambor, /cuellos y /capacidad
    # funcionen igual con datos de Vtiger que con el Excel manual.

    fechas_disponibles = sorted(df['fecha_entrega'].dropna().dt.strftime('%Y-%m-%d').unique().tolist())

    # --- Todas las ordenes (para selector dinamico de /dia) ---
    todas_ordenes = []
    for _, row in df.iterrows():
        fe = row['fecha_entrega']
        if pd.isna(fe):
            continue
        fc = row['fecha_creacion']
        color_toc = get_color_toc(fc, fe, today)
        fam_ord = str(row['familia']).strip()
        dias_p = FAMILIAS_PROMESA.get(fam_ord, 0)
        fecha_std = ''
        if pd.notna(fc) and dias_p > 0:
            _f, _d = fc, 0
            while _d < dias_p:
                _f += pd.Timedelta(days=1)
                if _f.weekday() != 6: _d += 1
            fecha_std = _f.strftime('%Y-%m-%d')
        todas_ordenes.append({
            'fecha': fe.strftime('%Y-%m-%d'),
            'fecha_entrega': fe.strftime('%d/%m/%Y'),
            'op': str(row['op_number']),
            'cliente': str(row['organizacion']),
            'referencia': str(row['referencia']),
            'maquina': str(row['maquina']),
            'etapa': str(row['etapa']).strip(),
            'tipo': get_tipo(row['referencia']),
            'mts': float(row['mts']),
            'familia': fam_ord,
            'color_toc': color_toc,
            'fecha_std': fecha_std,
            'promesa_dias': dias_p,
            'fecha_creacion': fc.strftime('%d/%m/%Y') if pd.notna(fc) else '',
        })

    # --- Capacidad RRC (Nilpeter 1/2, Kromia) ---
    maquinas_rrc = ['Nilpeter 1', 'Nilpeter 2', 'Kromia']
    capacidad_data = {}
    for m in maquinas_rrc:
        grp = df[df['maquina'] == m]
        por_fecha = grp.groupby(grp['fecha_entrega'].dt.strftime('%Y-%m-%d'))['mts'].sum()
        capacidad_data[m] = {
            'capacidad_dia': CAPACIDAD[m],
            'por_fecha': {k: round(float(v), 1) for k, v in por_fecha.items()},
        }

    # --- Cuellos de botella (carga por maquina real, con balanceo) ---
    carga_maq = {m: {'mts': 0, 'ordenes': []} for m in MAQUINAS_CAP}
    for _, row in df.iterrows():
        proceso = str(row['maquina_raw']).strip()
        etapa_ord = str(row['etapa']).strip()
        mts_ord = float(row['mts'])
        fe_ord = row['fecha_entrega']
        op_ord = str(row['op_number'])
        cli_ord = str(row['organizacion'])
        color_ord = get_color_toc(row['fecha_creacion'], fe_ord, today)
        maquinas_ord = get_maquinas_reales(proceso)

        en_impresora = any(m in ['NP1', 'NP2', 'Kromia'] for m in maquinas_ord)
        if en_impresora and etapa_ord not in ETAPAS_IMPRESION:
            maquinas_ord = [m for m in maquinas_ord if m not in ['NP1', 'NP2', 'Kromia']]

        maquinas_final = []
        for m in maquinas_ord:
            if m == 'REBOBINADORA_BALANCEAR':
                min_maq = min(REB_NAMES, key=lambda x: carga_maq[x]['mts'])
                maquinas_final.append(min_maq)
            else:
                maquinas_final.append(m)

        if 'TROQUELADORA ROTATIVA' in proceso.upper():
            min_trot = min(TROT_NAMES, key=lambda x: carga_maq[x]['mts'])
            maquinas_final.append(min_trot)

        for m in maquinas_final:
            if m in carga_maq:
                carga_maq[m]['mts'] += mts_ord
                carga_maq[m]['ordenes'].append({
                    'op': op_ord, 'cliente': cli_ord, 'mts': mts_ord,
                    'fecha': fe_ord.strftime('%d/%m/%Y') if pd.notna(fe_ord) else '',
                    'color': color_ord, 'tipo': get_tipo(row['referencia']), 'etapa': etapa_ord,
                })

    cuellos = {}
    for m, info in carga_maq.items():
        cap = MAQUINAS_CAP[m]
        mts_total = info['mts']
        dias_trabajo = round(mts_total / cap, 1) if cap > 0 else 0
        pct_cap = round(mts_total / (cap * 20) * 100, 1)
        fecha_prom = sumar_dias_lab(today, dias_trabajo)
        carga_por_fecha = {}
        ordenes_sorted = sorted(info['ordenes'], key=lambda x: x['fecha'])
        for o in ordenes_sorted:
            f = o['fecha']
            if f not in carga_por_fecha:
                carga_por_fecha[f] = {'mts': 0, 'ordenes': 0}
            carga_por_fecha[f]['mts'] += o['mts']
            carga_por_fecha[f]['ordenes'] += 1
        cuellos[m] = {
            'capacidad_dia': cap, 'mts_total': round(mts_total), 'dias_trabajo': dias_trabajo,
            'pct_cap': pct_cap, 'ordenes': ordenes_sorted, 'es_cuello': dias_trabajo > 10,
            'fecha_prometida': fecha_prom.strftime('%d/%m/%Y'), 'carga_por_fecha': carga_por_fecha,
        }

    # --- Datos simulacion tiempos estandar ---
    std_data = {m: {} for m in maquinas_rrc}
    for o in todas_ordenes:
        if o['maquina'] in maquinas_rrc and o.get('fecha_std') and o['etapa'] in ETAPAS_IMP_STD:
            f = o['fecha_std']
            std_data[o['maquina']][f] = std_data[o['maquina']].get(f, 0) + o['mts']
    fechas_std = sorted(set(f for m in maquinas_rrc for f in std_data[m].keys()))

    fechas_rrc = sorted(set(f for m in maquinas_rrc for f in capacidad_data[m]['por_fecha'].keys()))

    rrc_semana = {}
    for m in maquinas_rrc:
        rrc_semana[m] = {'capacidad': CAPACIDAD[m], 'por_dia': {}}
        for d in dias_str:
            planeado = capacidad_data[m]['por_fecha'].get(d, 0)
            cap = CAPACIDAD[m]
            pct = round(planeado / cap * 100, 1) if cap > 0 else 0
            rrc_semana[m]['por_dia'][d] = {
                'planeado': planeado, 'capacidad': cap, 'pct': pct,
                'estado': 'ok' if pct <= 100 else 'sobrecarga',
            }

    # --- Tambor General / urgentes ---
    prioridad_map = {}
    try:
        with open('data_store/liberacion.json', encoding='utf-8') as f:
            _lib = json.load(f)
            prioridad_map = _lib.get('prioridad_map', {})
    except Exception:
        pass

    urgentes = []
    todas_tambor = []
    for _, row in df_tambor.sort_values('fecha_entrega').iterrows():
        fe = row['fecha_entrega']
        fc_ord = row['fecha_creacion']
        color_toc = get_color_toc(fc_ord, fe, today)
        etapa_str = str(row['etapa']).strip()
        if pd.notna(fe) and pd.notna(fc_ord):
            # Normalizar a solo-fecha antes de restar (ver comentario en
            # get_color_toc): fc_ord trae hora, fe/today son solo fecha.
            _fc_n, _fe_n, _today_n = fc_ord.normalize(), fe.normalize(), today.normalize()
            dur = (_fe_n - _fc_n).days
            pct_buf = round((_today_n - _fc_n).days / dur * 100, 1) if dur > 0 else 0
        else:
            pct_buf = 0
        op_key = _norm_op(row['op_number'])
        prio_info = prioridad_map.get(op_key, {})
        if not prio_info:
            # Fallback de compatibilidad: liberacion.json pudo guardarse con
            # la clave corrupta por un bug historico en app.py (.rstrip('.0')
            # recorta caracteres, no el sufijo -- '104100' quedaba '1041').
            # Se prueba tambien esa variante para no perder prioridades ya
            # cargadas mientras no se resuba liberacion.json con la clave
            # correcta (ver fix pendiente en app.py: upload_liberacion()).
            _op_legacy = str(row['op_number']).strip().rstrip('.0')
            if _op_legacy != op_key:
                prio_info = prioridad_map.get(_op_legacy, {})
        pct_prio = prio_info.get('pct', '') if prio_info else ''
        tprod = row.get('tiempo_prod', '')

        ord_data = {
            'op': str(row['op_number']), 'cliente': str(row['organizacion']),
            'referencia': str(row['referencia']), 'maquina': str(row['maquina']),
            'etapa': etapa_str, 'tipo': get_tipo(row['referencia']),
            'fecha_entrega': fe.strftime('%d/%m/%Y') if pd.notna(fe) else '',
            'fecha_entrega_raw': fe.strftime('%Y-%m-%d') if pd.notna(fe) else '',
            'fecha_creacion': fc_ord.strftime('%d/%m/%Y') if pd.notna(fc_ord) else '',
            'dias_ofrecidos': int(dur) if pd.notna(fe) and pd.notna(fc_ord) else 0,
            'color_toc': color_toc, 'pct_prioridad': pct_prio, 'mts': float(row['mts']),
            'pct_buffer': pct_buf,
            'en_impresion': etapa_str in ETAPAS_IMP_STD,
            'tiempo_produccion': str(tprod).strip() if tprod and str(tprod).strip() else '—',
        }
        todas_tambor.append(ord_data)
        if pd.notna(fe) and fe <= today + pd.Timedelta(days=7):
            urgentes.append(ord_data)

    # --- Diagnostico de atraso (Nilpeter 1/2, Kromia) ---
    horas_nom, efic_nom, vel_nom = 22.75, 0.50, 35
    mins_turno_nom = horas_nom * 60 / 3
    mins_prod_nom = mins_turno_nom * efic_nom
    cap_turno_nom = round(mins_prod_nom * vel_nom)

    diagnostico = {}
    for mq in ['Nilpeter 1', 'Nilpeter 2', 'Kromia']:
        grp = df[(df['maquina'] == mq) & (df['etapa'].isin(ETAPAS_IMP_SET))]
        # Atrasada/pendiente se define contra la Fecha Entrega Prometida
        # (Produccion), igual que Ordenes Incumplidas - no contra la fecha
        # de entrega interna de la OP.
        atras = grp[grp['fecha_entrega'] < today]
        pend = grp[grp['fecha_entrega'] >= today]
        mts_a = round(float(atras['mts'].sum()))
        mts_p = round(float(pend['mts'].sum()))
        mts_t = mts_a + mts_p
        turnos_atras = round(mts_a / cap_turno_nom, 1) if cap_turno_nom > 0 else 0
        planes = []
        for dias_rec in [3, 5, 7, 10]:
            t_disp = dias_rec * 3
            mpt = round(mts_t / t_disp) if t_disp > 0 else 0
            vr = round(mpt / mins_prod_nom, 1) if mins_prod_nom > 0 else vel_nom
            er = round(mpt / (vel_nom * mins_turno_nom) * 100, 1) if vel_nom * mins_turno_nom > 0 else 0
            vc = round(min(vel_nom * 1.2, vr), 1)
            ec = round(mpt / (vc * mins_turno_nom) * 100, 1) if vc * mins_turno_nom > 0 else 0
            planes.append({'dias': dias_rec, 'turnos': t_disp, 'mts_por_turno': mpt,
                           'vel_req': vr, 'efic_req': er, 'vel_comb': vc, 'efic_comb': ec})
        diagnostico[mq] = {
            'mts_atrasados': mts_a, 'n_atrasadas': len(atras),
            'mts_pendientes': mts_p, 'n_pendientes': len(pend),
            'mts_total': mts_t, 'turnos_atrasados': turnos_atras,
            'dias_atrasados': round(turnos_atras / 3, 1),
            'cap_turno': cap_turno_nom, 'planes': planes,
        }

    colores_resumen = {
        'azul': sum(1 for o in todas_ordenes if o['color_toc'] == 'azul'),
        'verde': sum(1 for o in todas_ordenes if o['color_toc'] == 'verde'),
        'amarillo': sum(1 for o in todas_ordenes if o['color_toc'] == 'amarillo'),
        'rojo': sum(1 for o in todas_ordenes if o['color_toc'] == 'rojo'),
        'negro': sum(1 for o in todas_ordenes if o['color_toc'] == 'negro'),
    }
    dia_total = len(df[df['fecha_entrega'].dt.date == today.date()])

    result = {
        'updated_at': now_bogota.strftime('%d/%m/%Y %H:%M'),
        'source': 'vtiger_api_github_actions',
        'today': today.strftime('%Y-%m-%d'),
        'hoy': today.strftime('%Y-%m-%d'),
        'hoy_label': today.strftime('%d/%m/%Y'),
        'hoy_nombre': ['Lunes', 'Martes', 'Miércoles', 'Jueves', 'Viernes', 'Sábado', 'Domingo'][today.weekday()],
        'lun': lun.strftime('%d/%m'), 'vie': vie.strftime('%d/%m'),
        'maquinas': maquinas, 'dias_str': dias_str,
        'dias_label': dias_label, 'dias_nombre': dias_nombre,
        'pivot': pivot, 'totales_dia': totales_dia,
        'total_semana': total_semana, 'total_ordenes': len(df),
        'inc_total': len(incumplidas), 'inc_maq': inc_maq,
        'inc_etapas': inc_etapas, 'etapas_unicas': etapas_unicas,
        'inc_detalle': inc_detalle,
        'dia_max_nombre': dia_max_nombre,
        'dia_max_val': dia_max_val, 'dia_max_fecha': dia_max_fecha,
        # --- campos TOC para /dia, /tambor, /cuellos, /capacidad ---
        'urgentes': urgentes, 'urgentes_total': len(urgentes),
        'todas_tambor': todas_tambor,
        'colores_resumen': colores_resumen,
        'dia_total': dia_total, 'dia_por_maquina': {},
        'fechas_disponibles': fechas_disponibles,
        'todas_ordenes': todas_ordenes,
        'maquinas_rrc': maquinas_rrc,
        'std_data': std_data, 'fechas_std': fechas_std,
        'diagnostico': diagnostico,
        'cuellos': cuellos,
        'capacidad_data': capacidad_data,
        'fechas_rrc': fechas_rrc,
        'rrc_semana': rrc_semana,
    }

    os.makedirs('data_store', exist_ok=True)
    with open('data_store/latest.json', 'w', encoding='utf-8') as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"[sync] OK: {len(df)} registros, {result['updated_at']}")
    return True

if __name__ == '__main__':
    if not fetch_and_process():
        print("[sync] FALLO"); exit(1)
    print("[sync] EXITO")
