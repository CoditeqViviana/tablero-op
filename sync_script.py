"""
sync_script.py — Consulta Vtiger y genera latest.json.
Union por nombre de referencia (muchos a muchos).
"""
import os, json, re, requests, pandas as pd
from datetime import datetime
import pytz, warnings
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
    duracion = (fecha_entrega - fecha_creacion).days
    if duracion <= 0:
        return 'rojo'
    consumido = (today - fecha_creacion).days
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

    # Filtros Python (deben cumplirse AMBAS condiciones - AND, no OR):
    # - Asignado a Plataforma de Produccion (20x5)
    # - Entrega de Produccion != 'Habilitado' y != '1' (excluir habilitados)
    producciones = [p for p in producciones
                    if p.get(PROD['asignado'], '') == '20x5'
                    and p.get(PROD['entrega_prod'], '') not in ('Habilitado', '1')]
    print(f"[sync] Producciones filtradas: {len(producciones)}")

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

    def _op_num(o):
        try:
            return int(str(o.get(OP['number'], 0)).strip() or 0)
        except (ValueError, TypeError):
            return 0

    # Agrupamos las OPs por referencia (mismo criterio que arriba), ordenadas por
    # numero de OP (proxy de orden cronologico), para emparejar 1 a 1 con producciones.
    op_groups = defaultdict(list)
    for op in ordenes:
        proceso = op.get(OP['proceso'], '')
        if not proceso or proceso.strip() in ('', '-'):
            continue
        ref = op.get(OP['referencia'], '').strip()
        if ref:
            op_groups[ref].append(op)
    for ref in op_groups:
        op_groups[ref].sort(key=_op_num)

    # 3. Union 1 a 1: cada Produccion se empareja con UNA sola OP (relacion real es
    # 1:1, no muchos a muchos). Cuando una referencia se repite (reordenes), se
    # emparejan en el mismo orden cronologico en que fueron creadas ambos lados.
    rows = []
    sin_op = 0
    for ref, prods in prod_groups.items():
        ops_for_ref = op_groups.get(ref, [])
        for i, prod in enumerate(prods):
            if i >= len(ops_for_ref):
                sin_op += 1
                continue
            op = ops_for_ref[i]
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
            })

    print(f"[sync] {sin_op} Producciones sin OP, {len(rows)} filas OK")
    if not rows:
        print("[sync] ERROR: 0 filas construidas")
        return False

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
    for _, row in df.sort_values('fecha_entrega').iterrows():
        fe = row['fecha_entrega']
        fc_ord = row['fecha_creacion']
        color_toc = get_color_toc(fc_ord, fe, today)
        etapa_str = str(row['etapa']).strip()
        if pd.notna(fe) and pd.notna(fc_ord):
            dur = (fe - fc_ord).days
            pct_buf = round((today - fc_ord).days / dur * 100, 1) if dur > 0 else 0
        else:
            pct_buf = 0
        op_key = str(row['op_number']).strip().rstrip('.0')
        prio_info = prioridad_map.get(op_key, {})
        pct_prio = prio_info.get('pct', '') if prio_info else ''
        tprod = row.get('tiempo_prod', '')

        ord_data = {
            'op': str(row['op_number']), 'cliente': str(row['organizacion']),
            'referencia': str(row['referencia']), 'maquina': str(row['maquina']),
            'etapa': etapa_str, 'tipo': get_tipo(row['referencia']),
            'fecha_entrega': fe.strftime('%d/%m/%Y') if pd.notna(fe) else '',
            'fecha_entrega_raw': fe.strftime('%Y-%m-%d') if pd.notna(fe) else '',
            'fecha_creacion': fc_ord.strftime('%d/%m/%Y') if pd.notna(fc_ord) else '',
            'dias_ofrecidos': int((fe - fc_ord).days) if pd.notna(fe) and pd.notna(fc_ord) else 0,
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
