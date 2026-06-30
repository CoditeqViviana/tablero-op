"""
vtiger_service.py — Servicio para consultar Vtiger Cloud REST API v1
y construir un DataFrame compatible con el tablero de producción.

Usa Basic Auth (usuario:accessKey) contra la REST API de Vtiger Cloud.
"""

import os
import requests
import pandas as pd
from io import BytesIO
from datetime import datetime

# ─── CONFIGURACIÓN (variables de entorno en Render) ───
VTIGER_URL = os.environ.get('VTIGER_URL', 'https://coditeq.od2.vtiger.com')
VTIGER_USER = os.environ.get('VTIGER_USER', '')
VTIGER_KEY = os.environ.get('VTIGER_KEY', '')

API_BASE = f"{VTIGER_URL}/restapi/v1/vtiger/default"


def _auth():
    """Retorna tupla (user, key) para Basic Auth."""
    return (VTIGER_USER, VTIGER_KEY)


def vtiger_query(query_str, page_size=100):
    """
    Ejecuta una consulta SQL-like contra Vtiger REST API.
    Vtiger limita a 100 registros por consulta, así que paginamos.
    """
    all_records = []
    offset = 0

    while True:
        paged = f"{query_str} LIMIT {offset}, {page_size};"
        url = f"{API_BASE}/query"
        params = {'query': paged}

        try:
            r = requests.get(url, auth=_auth(), params=params, timeout=30)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            print(f"[vtiger_service] Error en query offset={offset}: {e}")
            break

        if not data.get('success'):
            print(f"[vtiger_service] Query falló: {data.get('error', {}).get('message', 'unknown')}")
            break

        records = data.get('result', [])
        all_records.extend(records)

        if len(records) < page_size:
            break

        offset += page_size

    return all_records


def vtiger_describe(module_name):
    """Describe un módulo para obtener metadatos de campos."""
    url = f"{API_BASE}/describe"
    params = {'elementType': module_name}
    try:
        r = requests.get(url, auth=_auth(), params=params, timeout=15)
        r.raise_for_status()
        data = r.json()
        if data.get('success'):
            return data['result']
    except Exception as e:
        print(f"[vtiger_service] Error describiendo {module_name}: {e}")
    return None


# ═══════════════════════════════════════════════════════════════
# MAPEO DE CAMPOS — Nombres reales verificados con /describe
# ═══════════════════════════════════════════════════════════════

# Campos de vtcmproduccion
PROD = {
    'referencia':       'fld_vtcmproduccionname',                          # Col 1
    'organizacion':     'cf_vtcmproduccion_organizacion',                  # Col 0
    'codigo_siigo':     'cf_vtcmproduccion_coodigosiigo',                  # Col 2
    'tipo':             'cf_vtcmproduccion_produccintipo',                  # Col 5
    'fecha_prometida':  'cf_vtcmproduccion_fechaentregaprometida',         # Col 10
    'fecha_confirm_mp': 'cf_vtcmproduccion_fechaconfirmacindeentregademateriaprima', # Col 11
    'etiquetas_rollo':  'cf_vtcmproduccion_nodeetiquetasporrollo',         # Col 15
    'fecha_liberacion': 'cf_vtcmproduccion_fecharealdeliberacin',          # Col 17
    'fecha_creacion':   'createdtime',                                      # Col 20
    'tiempo_prod':      'cf_vtcmproduccion_tiempoproduccin',               # Col 21
    'obs_ocop':         'cf_vtcmproduccion_observacionesocop',             # Col 23
    'estado':           'cf_vtcmproduccion_estado',                         # Para filtrar
}

# Campos de vtcmordendeproduccion
OP = {
    'number':           'vtcmordendeproduccionnumber',                      # Col 3
    'total_etiquetas':  'cf_vtcmordendeproduccion_totaletiquetas',          # Col 4
    'familia':          'cf_vtcmordendeproduccion_familia',                  # Col 6
    'proceso':          'cf_vtcmordendeproduccion_procesodeetiquetas',       # Col 7
    'mts_lineales':     'cf_vtcmordendeproduccion_totalmtslineales',         # Col 8
    'fecha_entrega':    'cf_vtcmordendeproduccion_fechadeentrega',           # Col 9
    'material':         'cf_vtcmordendeproduccion_material',                 # Col 12
    'adhesivo':         'cf_vtcmordendeproduccion_adhesivo',                 # Col 13
    'ancho':            'cf_vtcmordendeproduccion_ancho',                    # Col 14
    'centro_proceso':   'cf_vtcmordendeproduccion_centrodeproceso',          # Col 16
    'total_m2':         'cf_vtcmordendeproduccion_totalm2',                  # Col 18
    'tipo_orden':       'cf_vtcmordendeproduccion_tipodeorden',              # Col 19
    'z':                'cf_vtcmordendeproduccion_z',                        # Col 22
    'ref_produccion':   'cf_vtcmordendeproduccion_referenciadeproduccin',    # Relación → Produccion
    'estado_op':        'cf_vtcmordendeproduccion_estadoop',                 # Para filtrar
}


def fetch_produccion_data():
    """
    Consulta vtcmproduccion y vtcmordendeproduccion,
    los une y retorna un DataFrame con las 24 columnas del Excel.
    """

    # ─── 1. CONSULTAR ÓRDENES DE PRODUCCIÓN (activas) ───
    print("[vtiger_service] Consultando vtcmordendeproduccion...")
    op_query = "SELECT * FROM vtcmordendeproduccion"
    ordenes = vtiger_query(op_query)
    print(f"[vtiger_service] Órdenes obtenidas: {len(ordenes)}")

    if not ordenes:
        print("[vtiger_service] No se obtuvieron órdenes de producción")
        return None

    # ─── 2. CONSULTAR PRODUCCIONES ───
    print("[vtiger_service] Consultando vtcmproduccion...")
    prod_query = "SELECT * FROM vtcmproduccion"
    producciones = vtiger_query(prod_query)
    print(f"[vtiger_service] Producciones obtenidas: {len(producciones)}")

    # Índice de producciones por ID (formato "50xNNNN")
    prod_by_id = {p.get('id', ''): p for p in producciones}

    # ─── 3. UNIR Y CONSTRUIR FILAS ───
    rows = []
    sin_produccion = 0

    for op in ordenes:
        # Buscar la Produccion relacionada
        prod = None

        # Método 1: Campo de referencia directa en la OP
        ref_prod_id = op.get(OP['ref_produccion'], '')
        if ref_prod_id and ref_prod_id in prod_by_id:
            prod = prod_by_id[ref_prod_id]

        # Método 2: Buscar por cualquier campo que sea referencia "50x..."
        if not prod:
            for key, val in op.items():
                if isinstance(val, str) and val.startswith('50x') and val in prod_by_id:
                    prod = prod_by_id[val]
                    break

        if not prod:
            sin_produccion += 1
            prod = {}  # Usar dict vacío para no romper el flujo

        # Filtrar: solo órdenes activas (con proceso de etiquetas definido)
        proceso = op.get(OP['proceso'], '')
        if not proceso or proceso.strip() == '' or proceso.strip() == '-':
            continue

        # ─── CONSTRUIR FILA (24 columnas, mismo orden que el Excel) ───
        row = [
            # Col 0: Produccion Organizacion
            prod.get(PROD['organizacion'], ''),
            # Col 1: Produccion Referencia
            prod.get(PROD['referencia'], ''),
            # Col 2: Produccion Coodigo SIIGO
            prod.get(PROD['codigo_siigo'], ''),
            # Col 3: Orden de Produccion Number
            op.get(OP['number'], ''),
            # Col 4: Total Etiquetas
            _safe_num(op.get(OP['total_etiquetas'], 0)),
            # Col 5: Producción Tipo
            prod.get(PROD['tipo'], ''),
            # Col 6: Familia
            op.get(OP['familia'], ''),
            # Col 7: Proceso de Etiquetas (= etapa actual)
            proceso,
            # Col 8: Total mts lineales
            _safe_num(op.get(OP['mts_lineales'], 0)),
            # Col 9: Fecha de Entrega
            op.get(OP['fecha_entrega'], ''),
            # Col 10: Fecha Entrega Prometida
            prod.get(PROD['fecha_prometida'], ''),
            # Col 11: Fecha Confirmación MP
            prod.get(PROD['fecha_confirm_mp'], ''),
            # Col 12: Material
            op.get(OP['material'], ''),
            # Col 13: Adhesivo
            op.get(OP['adhesivo'], ''),
            # Col 14: Ancho
            _safe_num(op.get(OP['ancho'], 0)),
            # Col 15: No de etiquetas por rollo
            _safe_num(prod.get(PROD['etiquetas_rollo'], 0)),
            # Col 16: Centro de Proceso
            op.get(OP['centro_proceso'], ''),
            # Col 17: Fecha Real Liberación
            prod.get(PROD['fecha_liberacion'], ''),
            # Col 18: Total m2
            _safe_num(op.get(OP['total_m2'], 0)),
            # Col 19: Tipo de Orden
            op.get(OP['tipo_orden'], ''),
            # Col 20: Fecha de Creación (formato Vtiger: "YYYY-MM-DD HH:MM:SS")
            _format_createdtime(prod.get(PROD['fecha_creacion'], '')),
            # Col 21: Tiempo Producción
            prod.get(PROD['tiempo_prod'], ''),
            # Col 22: Z=
            op.get(OP['z'], ''),
            # Col 23: Observaciones OC OP
            prod.get(PROD['obs_ocop'], ''),
        ]
        rows.append(row)

    if sin_produccion > 0:
        print(f"[vtiger_service] {sin_produccion} OPs sin Producción relacionada")

    if not rows:
        print("[vtiger_service] No se construyeron filas después del filtrado")
        return None

    # ─── 4. CREAR DATAFRAME ───
    columns = [
        'Produccion Organizacion',
        'Produccion Referencia',
        'Produccion Coodigo SIIGO',
        'Orden de Produccion Orden de producción  Number',
        'Orden de Produccion Total Etiquetas',
        'Produccion Producción Tipo',
        'Orden de Produccion Familia',
        'Orden de Produccion Proceso de Etiquetas',
        'Orden de Produccion Total mts lineales',
        'Orden de Produccion Fecha de Entrega',
        'Produccion Fecha Entrega Prometida',
        'Produccion Fecha Confirmación de Entrega de Materia Prima',
        'Orden de Produccion Material',
        'Orden de Produccion Adhesivo',
        'Orden de Produccion Ancho',
        'Produccion No de etiquetas por rollo',
        'Orden de Produccion Centro de Proceso',
        'Produccion Fecha Real de Liberación - Focus',
        'Orden de Produccion Total m2',
        'Orden de Produccion Tipo de Orden',
        'Produccion Fecha de Creación',
        'Produccion Tiempo Producción',
        'Orden de Produccion Z=',
        'Produccion Observaciones OC OP',
    ]

    df = pd.DataFrame(rows, columns=columns)
    print(f"[vtiger_service] DataFrame creado: {len(df)} filas x {len(df.columns)} columnas")
    return df


def dataframe_to_excel_bytes(df):
    """Convierte un DataFrame a bytes de Excel (para reutilizar process_excel)."""
    buffer = BytesIO()
    df.to_excel(buffer, index=False, engine='openpyxl')
    buffer.seek(0)
    return buffer


def _safe_num(val):
    """Convierte a número de forma segura."""
    try:
        if val is None or str(val).strip() == '':
            return 0
        return float(val)
    except (ValueError, TypeError):
        return 0


def _format_createdtime(vtiger_datetime):
    """
    Convierte el formato de fecha de Vtiger (YYYY-MM-DD HH:MM:SS)
    al formato que espera process_excel (DD-MM-YYYY HH:MM AM/PM).
    """
    if not vtiger_datetime or str(vtiger_datetime).strip() == '':
        return ''
    try:
        dt = datetime.strptime(str(vtiger_datetime).strip()[:19], '%Y-%m-%d %H:%M:%S')
        return dt.strftime('%d-%m-%Y %I:%M %p')
    except (ValueError, TypeError):
        return str(vtiger_datetime)


# ─── UTILIDAD: Listar campos de un módulo ───
def list_module_fields(module_name):
    """Devuelve dict {field_name: field_label} para debug/mapeo."""
    desc = vtiger_describe(module_name)
    if not desc:
        return {}
    return {f['name']: f['label'] for f in desc.get('fields', [])}
