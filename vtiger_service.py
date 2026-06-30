"""
vtiger_service.py — Vtiger Cloud REST API v1 (optimizado para Render Free)
"""
import os
import requests
import pandas as pd
from io import BytesIO
from datetime import datetime

VTIGER_URL = os.environ.get('VTIGER_URL', 'https://coditeq.od2.vtiger.com')
VTIGER_USER = os.environ.get('VTIGER_USER', '')
VTIGER_KEY = os.environ.get('VTIGER_KEY', '')
API_BASE = f"{VTIGER_URL}/restapi/v1/vtiger/default"

def _auth():
    return (VTIGER_USER, VTIGER_KEY)

def vtiger_query(query_str, page_size=100):
    all_records = []
    offset = 0
    while True:
        paged = f"{query_str} LIMIT {offset}, {page_size};"
        url = f"{API_BASE}/query"
        params = {'query': paged}
        try:
            r = requests.get(url, auth=_auth(), params=params, timeout=60, verify=False)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"[vtiger] Error query offset={offset}: {e}")
            break
        if not data.get('success'):
            msg = data.get('error',{}).get('message','?')
            print(f"[vtiger] Query fallo: {msg}")
            break
        records = data.get('result', [])
        all_records.extend(records)
        if len(records) < page_size:
            break
        offset += page_size
    return all_records

def vtiger_describe(module_name):
    url = f"{API_BASE}/describe"
    params = {'elementType': module_name}
    try:
        r = requests.get(url, auth=_auth(), params=params, timeout=15, verify=False)
        r.raise_for_status()
        data = r.json()
        if data.get('success'):
            return data['result']
    except Exception as e:
        print(f"[vtiger] Error describe {module_name}: {e}")
    return None

# Campos que necesitamos de cada modulo (solo los necesarios, no todos)
PROD_SELECT = (
    "id,fld_vtcmproduccionname,cf_vtcmproduccion_organizacion,"
    "cf_vtcmproduccion_coodigosiigo,cf_vtcmproduccion_produccintipo,"
    "cf_vtcmproduccion_fechaentregaprometida,"
    "cf_vtcmproduccion_fechaconfirmacindeentregademateriaprima,"
    "cf_vtcmproduccion_nodeetiquetasporrollo,"
    "cf_vtcmproduccion_fecharealdeliberacin,createdtime,"
    "cf_vtcmproduccion_tiempoproduccin,cf_vtcmproduccion_observacionesocop,"
    "cf_vtcmproduccion_estado"
)

OP_SELECT = (
    "id,vtcmordendeproduccionnumber,cf_vtcmordendeproduccion_totaletiquetas,"
    "cf_vtcmordendeproduccion_familia,cf_vtcmordendeproduccion_procesodeetiquetas,"
    "cf_vtcmordendeproduccion_totalmtslineales,cf_vtcmordendeproduccion_fechadeentrega,"
    "cf_vtcmordendeproduccion_material,cf_vtcmordendeproduccion_adhesivo,"
    "cf_vtcmordendeproduccion_ancho,cf_vtcmordendeproduccion_centrodeproceso,"
    "cf_vtcmordendeproduccion_totalm2,cf_vtcmordendeproduccion_tipodeorden,"
    "cf_vtcmordendeproduccion_z,cf_vtcmordendeproduccion_referenciadeproduccin,"
    "cf_vtcmordendeproduccion_estadoop"
)

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
}

OP = {
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
    'ref_produccion': 'cf_vtcmordendeproduccion_referenciadeproduccin',
    'estado_op': 'cf_vtcmordendeproduccion_estadoop',
}

def fetch_produccion_data():
    import warnings
    warnings.filterwarnings('ignore')

    # Solo traer producciones activas (Programada)
    print("[vtiger] Consultando producciones activas...")
    producciones = vtiger_query(
        f"SELECT {PROD_SELECT} FROM vtcmproduccion "
        f"WHERE cf_vtcmproduccion_tiempoproduccin = 'Programada'"
    )
    print(f"[vtiger] Producciones: {len(producciones)}")

    if not producciones:
        # Fallback: no entregadas
        print("[vtiger] Fallback: no entregadas...")
        producciones = vtiger_query(
            f"SELECT {PROD_SELECT} FROM vtcmproduccion "
            f"WHERE cf_vtcmproduccion_estado != 'Entregado'"
        )
        print(f"[vtiger] Producciones fallback: {len(producciones)}")

    if not producciones:
        return None

    prod_by_id = {p.get('id', ''): p for p in producciones}
    print(f"[vtiger] IDs de produccion: {len(prod_by_id)}")

    # Solo traer OPs con proceso activo
    print("[vtiger] Consultando ordenes de produccion...")
    ordenes = vtiger_query(
        f"SELECT {OP_SELECT} FROM vtcmordendeproduccion "
        f"WHERE cf_vtcmordendeproduccion_procesodeetiquetas != ''"
    )
    print(f"[vtiger] Ordenes: {len(ordenes)}")

    if not ordenes:
        return None

    rows = []
    sin_prod = 0
    for op in ordenes:
        prod = None
        ref_id = op.get(OP['ref_produccion'], '')
        if ref_id and ref_id in prod_by_id:
            prod = prod_by_id[ref_id]
        if not prod:
            for key, val in op.items():
                if isinstance(val, str) and val.startswith('50x') and val in prod_by_id:
                    prod = prod_by_id[val]
                    break
        if not prod:
            sin_prod += 1
            continue

        proceso = op.get(OP['proceso'], '')
        if not proceso or proceso.strip() in ('', '-'):
            continue

        row = [
            prod.get(PROD['organizacion'], ''),
            prod.get(PROD['referencia'], ''),
            prod.get(PROD['codigo_siigo'], ''),
            op.get(OP['number'], ''),
            _safe_num(op.get(OP['total_etiquetas'], 0)),
            prod.get(PROD['tipo'], ''),
            op.get(OP['familia'], ''),
            proceso,
            _safe_num(op.get(OP['mts_lineales'], 0)),
            op.get(OP['fecha_entrega'], ''),
            prod.get(PROD['fecha_prometida'], ''),
            prod.get(PROD['fecha_confirm_mp'], ''),
            op.get(OP['material'], ''),
            op.get(OP['adhesivo'], ''),
            _safe_num(op.get(OP['ancho'], 0)),
            _safe_num(prod.get(PROD['etiquetas_rollo'], 0)),
            op.get(OP['centro_proceso'], ''),
            prod.get(PROD['fecha_liberacion'], ''),
            _safe_num(op.get(OP['total_m2'], 0)),
            op.get(OP['tipo_orden'], ''),
            _format_createdtime(prod.get(PROD['fecha_creacion'], '')),
            prod.get(PROD['tiempo_prod'], ''),
            op.get(OP['z'], ''),
            prod.get(PROD['obs_ocop'], ''),
        ]
        rows.append(row)

    print(f"[vtiger] {sin_prod} OPs sin Produccion, {len(rows)} filas OK")
    if not rows:
        return None

    columns = [
        'Produccion Organizacion','Produccion Referencia','Produccion Coodigo SIIGO',
        'Orden de Produccion Orden de produccion  Number','Orden de Produccion Total Etiquetas',
        'Produccion Produccion Tipo','Orden de Produccion Familia',
        'Orden de Produccion Proceso de Etiquetas','Orden de Produccion Total mts lineales',
        'Orden de Produccion Fecha de Entrega','Produccion Fecha Entrega Prometida',
        'Produccion Fecha Confirmacion de Entrega de Materia Prima',
        'Orden de Produccion Material','Orden de Produccion Adhesivo',
        'Orden de Produccion Ancho','Produccion No de etiquetas por rollo',
        'Orden de Produccion Centro de Proceso','Produccion Fecha Real de Liberacion - Focus',
        'Orden de Produccion Total m2','Orden de Produccion Tipo de Orden',
        'Produccion Fecha de Creacion','Produccion Tiempo Produccion',
        'Orden de Produccion Z=','Produccion Observaciones OC OP',
    ]
    df = pd.DataFrame(rows, columns=columns)
    print(f"[vtiger] DataFrame: {len(df)} filas")
    return df

def dataframe_to_excel_bytes(df):
    buffer = BytesIO()
    df.to_excel(buffer, index=False, engine='openpyxl')
    buffer.seek(0)
    return buffer

def _safe_num(val):
    try:
        if val is None or str(val).strip() == '':
            return 0
        return float(val)
    except (ValueError, TypeError):
        return 0

def _format_createdtime(vtiger_datetime):
    if not vtiger_datetime or str(vtiger_datetime).strip() == '':
        return ''
    try:
        dt = datetime.strptime(str(vtiger_datetime).strip()[:19], '%Y-%m-%d %H:%M:%S')
        return dt.strftime('%d-%m-%Y %I:%M %p')
    except (ValueError, TypeError):
        return str(vtiger_datetime)

def list_module_fields(module_name):
    desc = vtiger_describe(module_name)
    if not desc:
        return {}
    return {f['name']: f['label'] for f in desc.get('fields', [])}
