"""
sync_script.py — Consulta Vtiger y genera latest.json.
Union por nombre de referencia (muchos a muchos).
"""
import os, json, requests, pandas as pd
from datetime import datetime
import pytz, warnings
warnings.filterwarnings('ignore')

VTIGER_URL = 'https://coditeq.od2.vtiger.com'
VTIGER_USER = os.environ.get('VTIGER_USER', '')
VTIGER_KEY = os.environ.get('VTIGER_KEY', '')
API_BASE = f"{VTIGER_URL}/restapi/v1/vtiger/default"
BOGOTA = pytz.timezone('America/Bogota')

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
    # 1. Producciones con filtro fecha
    print("[sync] Consultando producciones (fecha > 2024-12-31)...")
    producciones = vtiger_query(
        "SELECT * FROM vtcmproduccion WHERE cf_vtcmproduccion_fechaentregaprometida > '2024-12-31'"
    )
    print(f"[sync] Producciones brutas: {len(producciones)}")

    if not producciones:
        print("[sync] ERROR: Sin producciones")
        return False

    # Filtros Python:
    # - Entrega de Produccion != 'Habilitado' y != '1' (excluir habilitados)
    # - Asignado a Plataforma de Produccion (20x5)
    producciones = [p for p in producciones
                    if p.get(PROD['entrega_prod'], '') not in ('Habilitado', '1')
                    or p.get(PROD['asignado'], '') == '20x5']
    print(f"[sync] Producciones filtradas: {len(producciones)}")

    # Indexar por REFERENCIA (nombre) para la union
    prod_by_ref = {}
    for p in producciones:
        ref = p.get(PROD['referencia'], '').strip()
        if ref:
            # Si hay duplicados, quedarse con el de Plataforma (20x5)
            existing = prod_by_ref.get(ref)
            if not existing or p.get(PROD['asignado'], '') == '20x5':
                prod_by_ref[ref] = p
    print(f"[sync] Referencias unicas: {len(prod_by_ref)}")

    # 2. Ordenes con filtro fecha
    print("[sync] Consultando ordenes (fecha > 2024-12-31)...")
    ordenes = vtiger_query(
        "SELECT * FROM vtcmordendeproduccion WHERE cf_vtcmordendeproduccion_fechadeentrega > '2024-12-31'"
    )
    print(f"[sync] Ordenes: {len(ordenes)}")

    if not ordenes:
        print("[sync] ERROR: Sin ordenes")
        return False

    # 3. Unir por REFERENCIA
    rows = []
    sin_prod = 0
    for op in ordenes:
        proceso = op.get(OP['proceso'], '')
        if not proceso or proceso.strip() in ('', '-'):
            continue

        # Buscar produccion por nombre de referencia
        op_ref = op.get(OP['referencia'], '').strip()
        prod = prod_by_ref.get(op_ref)

        if not prod:
            sin_prod += 1
            continue

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

    print(f"[sync] {sin_prod} OPs sin match, {len(rows)} filas OK")
    if not rows:
        print("[sync] ERROR: 0 filas construidas")
        return False

    # 4. Procesar
    df = pd.DataFrame(rows)
    df['fecha_entrega'] = pd.to_datetime(df['fecha_entrega_raw'], errors='coerce')
    df['fecha_creacion'] = pd.to_datetime(df['fecha_creacion_raw'], format='%d-%m-%Y %I:%M %p', errors='coerce')
    df['maquina'] = df['maquina_raw'].astype(str).str.split(',').str[0].str.strip()

    now_bogota = datetime.now(BOGOTA)
    today = pd.Timestamp(now_bogota.date())
    days_until_monday = (7 - today.weekday()) % 7
    if days_until_monday == 0: days_until_monday = 7
    lun = today + pd.Timedelta(days=days_until_monday)
    if today.weekday() < 5:
        lun = today - pd.Timedelta(days=today.weekday())
    vie = lun + pd.Timedelta(days=4)
    incumplidas = df[df['fecha_entrega'] < today]
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

    total_semana = sum(totales_dia.values())
    inc_maq = incumplidas['maquina'].value_counts().to_dict()
    if totales_dia:
        dia_max_key = max(totales_dia, key=totales_dia.get)
        dia_max_idx = dias_str.index(dia_max_key) if dia_max_key in dias_str else 0
        dia_max_nombre = dias_nombre[dia_max_idx] if dia_max_idx < len(dias_nombre) else ''
        dia_max_val = totales_dia[dia_max_key]
        dia_max_fecha = dias_label[dia_max_idx] if dia_max_idx < len(dias_label) else ''
    else:
        dia_max_nombre, dia_max_val, dia_max_fecha = '', 0, ''

    result = {
        'updated_at': now_bogota.strftime('%d/%m/%Y %H:%M'),
        'source': 'vtiger_api_github_actions',
        'today': today.strftime('%Y-%m-%d'),
        'hoy': today.strftime('%Y-%m-%d'),
        'lun': lun.strftime('%d/%m'), 'vie': vie.strftime('%d/%m'),
        'maquinas': maquinas, 'dias_str': dias_str,
        'dias_label': dias_label, 'dias_nombre': dias_nombre,
        'pivot': pivot, 'totales_dia': totales_dia,
        'total_semana': total_semana, 'total_ordenes': len(df),
        'inc_total': len(incumplidas), 'inc_maq': inc_maq,
        'dia_max_nombre': dia_max_nombre,
        'dia_max_val': dia_max_val, 'dia_max_fecha': dia_max_fecha,
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
