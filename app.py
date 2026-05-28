from flask import Flask, render_template, request, jsonify
import pandas as pd
import json
import os
from datetime import datetime
import pytz

app = Flask(__name__)
UPLOAD_FOLDER = 'data'
DATA_FILE = os.path.join(UPLOAD_FOLDER, 'latest.json')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
BOGOTA = pytz.timezone('America/Bogota')

# Capacidad por maquina (metros por dia, 3 turnos)
CAPACIDAD = {  # 7900 metros/turno x 3 turnos

    'Nilpeter 1': 7900 * 3,  # 7900 mts/turno x 3 turnos = 23700 mts/dia
    'Nilpeter 2': 7900 * 3,
    'Kromia':     7900 * 3,
}

def get_tipo(ref):
    r = str(ref).strip().upper()
    if r.startswith('BL'): return 'Blanca'
    if r.startswith('IC') or r.startswith('IS'): return 'Impresa'
    if r.startswith('FD'): return 'Fondo'
    return 'Otro'

def process_excel(file):
    df = pd.read_excel(file, header=0)
    df['fecha_entrega'] = pd.to_datetime(df.iloc[:, 9], errors='coerce')
    df['maquina'] = df.iloc[:, 16].astype(str).str.split(',').str[0].str.strip()
    df['etapa'] = df.iloc[:, 7].astype(str).str.strip()
    df['mts'] = pd.to_numeric(df.iloc[:, 8], errors='coerce').fillna(0)

    now_bogota = datetime.now(BOGOTA)
    today = pd.Timestamp(now_bogota.date())

    if today.weekday() < 5:
        lun = today - pd.Timedelta(days=today.weekday())
    else:
        lun = today + pd.Timedelta(days=(7 - today.weekday()))
    vie = lun + pd.Timedelta(days=4)

    incumplidas_df = df[df['fecha_entrega'] < today].copy()
    maquinas = sorted(df['maquina'].dropna().unique().tolist())
    dias = pd.date_range(lun, vie, freq='B')
    dias_str = [d.strftime('%Y-%m-%d') for d in dias]
    dias_label = [d.strftime('%d/%m') for d in dias]
    dias_nombre = ['Lunes', 'Martes', 'Miércoles', 'Jueves', 'Viernes']

    pivot = {}
    totales_dia = {d: 0 for d in dias_str}
    for m in maquinas:
        pivot[m] = {}
        for d in dias_str:
            n = len(df[(df['maquina'] == m) & (df['fecha_entrega'].dt.strftime('%Y-%m-%d') == d)])
            pivot[m][d] = n
            totales_dia[d] += n

    total_semana = sum(totales_dia.values())

    if totales_dia:
        dia_max_key = max(totales_dia, key=totales_dia.get)
        dia_max_idx = dias_str.index(dia_max_key) if dia_max_key in dias_str else 0
        dia_max_nombre = dias_nombre[dia_max_idx] if dia_max_idx < len(dias_nombre) else ''
        dia_max_val = totales_dia[dia_max_key]
        dia_max_fecha = dias_label[dia_max_idx] if dia_max_idx < len(dias_label) else ''
    else:
        dia_max_nombre, dia_max_val, dia_max_fecha = '', 0, ''

    # Incumplidas
    inc_detalle = []
    for _, row in incumplidas_df.iterrows():
        fe = row['fecha_entrega']
        tipo = get_tipo(row.iloc[1])
        inc_detalle.append({
            'cliente': str(row.iloc[0]),
            'referencia': str(row.iloc[1]),
            'op': str(row.iloc[3]),
            'etapa': str(row.iloc[7]).strip(),
            'fecha_entrega': fe.strftime('%d/%m/%Y') if pd.notna(fe) else '',
            'tipo': tipo,
        })
    inc_detalle.sort(key=lambda x: x['fecha_entrega'])

    etapas_dict = {}
    for r in inc_detalle:
        e = r['etapa']
        if e not in etapas_dict:
            etapas_dict[e] = {'total': 0, 'ordenes': []}
        etapas_dict[e]['total'] += 1
        etapas_dict[e]['ordenes'].append({'cliente': r['cliente'], 'tipo': r['tipo'], 'op': r['op']})
    inc_etapas = dict(sorted(etapas_dict.items(), key=lambda x: x[1]['total'], reverse=True))
    etapas_unicas = sorted(etapas_dict.keys())

    # Todas las fechas disponibles
    fechas_disponibles = sorted(df['fecha_entrega'].dropna().dt.strftime('%Y-%m-%d').unique().tolist())

    # Todas las ordenes para selector dinámico
    todas_ordenes = []
    for _, row in df.iterrows():
        fe = row['fecha_entrega']
        if pd.isna(fe): continue
        todas_ordenes.append({
            'fecha': fe.strftime('%Y-%m-%d'),
            'op': str(row.iloc[3]),
            'cliente': str(row.iloc[0]),
            'referencia': str(row.iloc[1]),
            'maquina': str(row['maquina']),
            'etapa': str(row.iloc[7]).strip(),
            'tipo': get_tipo(row.iloc[1]),
            'mts': float(row['mts']),
        })

    # === CAPACIDAD RRC ===
    maquinas_rrc = ['Nilpeter 1', 'Nilpeter 2', 'Kromia']

    # Metros planeados por maquina por fecha
    capacidad_data = {}
    for m in maquinas_rrc:
        grp = df[df['maquina'] == m]
        por_fecha = grp.groupby(grp['fecha_entrega'].dt.strftime('%Y-%m-%d'))['mts'].sum()
        capacidad_data[m] = {
            'capacidad_dia': CAPACIDAD[m],
            'por_fecha': {k: round(float(v), 1) for k, v in por_fecha.items()},
        }

    # Fechas con al menos una orden en las 3 maquinas RRC
    fechas_rrc = sorted(set(
        f for m in maquinas_rrc
        for f in capacidad_data[m]['por_fecha'].keys()
    ))

    # Resumen semanal RRC (semana actual)
    rrc_semana = {}
    for m in maquinas_rrc:
        rrc_semana[m] = {
            'capacidad': CAPACIDAD[m],
            'por_dia': {}
        }
        for d in dias_str:
            planeado = capacidad_data[m]['por_fecha'].get(d, 0)
            cap = CAPACIDAD[m]
            pct = round(planeado / cap * 100, 1) if cap > 0 else 0
            rrc_semana[m]['por_dia'][d] = {
                'planeado': planeado,
                'capacidad': cap,
                'pct': pct,
                'estado': 'ok' if pct <= 100 else 'sobrecarga',
            }

    result = {
        'updated_at': now_bogota.strftime('%d/%m/%Y %H:%M'),
        'hoy': today.strftime('%Y-%m-%d'),
        'hoy_label': today.strftime('%d/%m/%Y'),
        'hoy_nombre': ['Lunes','Martes','Miércoles','Jueves','Viernes','Sábado','Domingo'][today.weekday()],
        'lun': lun.strftime('%d/%m'),
        'vie': vie.strftime('%d/%m'),
        'dias_str': dias_str,
        'dias_label': dias_label,
        'dias_nombre': dias_nombre,
        'maquinas': maquinas,
        'pivot': pivot,
        'totales_dia': totales_dia,
        'total_semana': total_semana,
        'total_ordenes': len(df),
        'inc_total': len(incumplidas_df),
        'inc_etapas': inc_etapas,
        'inc_detalle': inc_detalle,
        'etapas_unicas': etapas_unicas,
        'dia_total': len(df[df['fecha_entrega'].dt.date == today.date()]),
        'dia_por_maquina': {},
        'fechas_disponibles': fechas_disponibles,
        'todas_ordenes': todas_ordenes,
        'maquinas_rrc': maquinas_rrc,
        'capacidad_data': capacidad_data,
        'fechas_rrc': fechas_rrc,
        'rrc_semana': rrc_semana,
        'dia_max_nombre': dia_max_nombre,
        'dia_max_val': dia_max_val,
        'dia_max_fecha': dia_max_fecha,
    }
    return result

@app.route('/')
def index():
    data = None
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, encoding='utf-8') as f:
            data = json.load(f)
    return render_template('index.html', data=data)

@app.route('/dia')
def dia_view():
    data = None
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, encoding='utf-8') as f:
            data = json.load(f)
    return render_template('dia.html', data=data)

@app.route('/capacidad')
def capacidad_view():
    data = None
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, encoding='utf-8') as f:
            data = json.load(f)
    return render_template('capacidad.html', data=data)

@app.route('/upload', methods=['POST'])
def upload():
    if 'file' not in request.files:
        return jsonify({'error': 'No se recibio archivo'}), 400
    file = request.files['file']
    if not file.filename.endswith(('.xlsx', '.xls', '.xlsm')):
        return jsonify({'error': 'Solo se aceptan archivos Excel'}), 400
    try:
        result = process_excel(file)
        with open(DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False)
        return jsonify({'ok': True, 'updated_at': result['updated_at']})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(debug=False)
