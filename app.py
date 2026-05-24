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

def process_excel(file):
    df = pd.read_excel(file, header=0)
    df['fecha_entrega'] = pd.to_datetime(df.iloc[:, 9], errors='coerce')
    df['fecha_creacion'] = pd.to_datetime(df.iloc[:, 20], format='%d-%m-%Y %I:%M %p', errors='coerce')
    df['maquina'] = df.iloc[:, 16].astype(str).str.split(',').str[0].str.strip()
    df['etapa'] = df.iloc[:, 7].astype(str).str.strip()

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
    inc_maq = incumplidas_df['maquina'].value_counts().to_dict()

    if totales_dia:
        dia_max_key = max(totales_dia, key=totales_dia.get)
        dia_max_idx = dias_str.index(dia_max_key) if dia_max_key in dias_str else 0
        dia_max_nombre = dias_nombre[dia_max_idx] if dia_max_idx < len(dias_nombre) else ''
        dia_max_val = totales_dia[dia_max_key]
        dia_max_fecha = dias_label[dia_max_idx] if dia_max_idx < len(dias_label) else ''
    else:
        dia_max_nombre, dia_max_val, dia_max_fecha = '', 0, ''

    # Detalle ordenes incumplidas
    inc_detalle = []
    for _, row in incumplidas_df.iterrows():
        fe = row['fecha_entrega']
        inc_detalle.append({
            'cliente': str(row.iloc[0]),
            'referencia': str(row.iloc[1]),
            'op': str(row.iloc[3]),
            'etapa': str(row.iloc[7]),
            'fecha_entrega': fe.strftime('%d/%m/%Y') if pd.notna(fe) else '',
        })
    # Ordenar por fecha mas antigua primero
    inc_detalle.sort(key=lambda x: x['fecha_entrega'])

    result = {
        'updated_at': now_bogota.strftime('%d/%m/%Y %H:%M'),
        'today': today.strftime('%Y-%m-%d'),
        'lun': lun.strftime('%d/%m'),
        'vie': vie.strftime('%d/%m'),
        'maquinas': maquinas,
        'dias_str': dias_str,
        'dias_label': dias_label,
        'dias_nombre': dias_nombre,
        'pivot': pivot,
        'totales_dia': totales_dia,
        'total_semana': total_semana,
        'total_ordenes': len(df),
        'inc_total': len(incumplidas_df),
        'inc_maq': inc_maq,
        'inc_detalle': inc_detalle,
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

@app.route('/upload', methods=['POST'])
def upload():
    if 'file' not in request.files:
        return jsonify({'error': 'No se recibio archivo'}), 400
    file = request.files['file']
    if not file.filename.endswith(('.xlsx', '.xls')):
        return jsonify({'error': 'Solo se aceptan archivos Excel (.xlsx, .xls)'}), 400
    try:
        result = process_excel(file)
        with open(DATA_FILE, 'w', encoding='utf-8') as f:
            json.dump(result, f, ensure_ascii=False)
        return jsonify({'ok': True, 'updated_at': result['updated_at']})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

if __name__ == '__main__':
    app.run(debug=False)
