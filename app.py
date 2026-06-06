from flask import Flask, render_template, request, jsonify
import pandas as pd
import json
import os
from datetime import datetime
import pytz

app = Flask(__name__)
app.jinja_env.filters['format_number'] = lambda v: f'{int(v):,}'.replace(',', '.')
UPLOAD_FOLDER = 'data'
DATA_FILE = os.path.join(UPLOAD_FOLDER, 'latest.json')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
BOGOTA = pytz.timezone('America/Bogota')

# Capacidad: 22.75h * 50% eficiencia * 60min * 35m/min = 23,888 mts/dia
CAP_DEFAULT = round(22.75 * 0.50 * 60 * 35)
CAPACIDAD = {
    'Nilpeter 1': CAP_DEFAULT,
    'Nilpeter 2': CAP_DEFAULT,
    'Kromia':     CAP_DEFAULT,
}

# Capacidades diarias por máquina (metros/día)
MAQUINAS_CAP = {
    'NP1': 23100, 'NP2': 23100, 'Kromia': 23100,
    'Rebobinadora 1': 16500, 'Rebobinadora 2': 16500,
    'Rebobinadora 3': 16500, 'Rebobinadora (T) 4': 16500,
    'Rebobinadora KM': 16500,
    'Troqueladora 1': 16500, 'Troqueladora 2': 16500,
    'Troqueladora 3': 16500, 'Troqueladora Aut 4': 16500,
    'Troqueladora Plana': 16500, 'Plegadora': 16500,
}
TROT_NAMES = ['Troqueladora 1','Troqueladora 2','Troqueladora 3','Troqueladora Aut 4']

def get_maquinas_reales(proceso):
    """Devuelve lista de maquinas reales para una orden segun su proceso"""
    maquinas = []
    p = proceso.upper()
    if 'NILPETER 1' in p: maquinas.append('NP1')
    if 'NILPETER 2' in p: maquinas.append('NP2')
    if 'KROMIA' in p: maquinas.append('Kromia')
    if 'REBOBINADORA' in p and 'MOTEX' not in p:
        maquinas.append('REBOBINADORA_BALANCEAR')  # se asigna balanceado luego
    if 'TROQUELADORA PLANA' in p: maquinas.append('Troqueladora Plana')
    if 'PLEGADORA' in p: maquinas.append('Plegadora')
    # EMPAQUE no es Plegadora - se omite por no estar en la lista de maquinas RRC
    return maquinas

def get_tipo(ref):
    r = str(ref).strip().upper()
    if r.startswith('BL'): return 'Blanca'
    if r.startswith('IC') or r.startswith('IS'): return 'Impresa'
    if r.startswith('FD'): return 'Fondo'
    return 'Otro'

# Amortiguadores por familia
FAMILIAS_AMORT = {
    'F1':2,'F2':2,'F3':8,'F4':1,'F5':3,'F6':3,'F7':4,'F8':3,
    'F9':5,'F10':4,'F11':5,'F12':5,'F13':3,'F14':3,'F15':4,
    'F16':3,'F17':8,'F18':8
}

FAMILIAS_PROMESA = {
    'F1':5,'F2':4,'F3':16,'F4':2,'F5':7,'F6':6,'F7':8,'F8':3,
    'F9':10,'F10':9,'F11':11,'F12':5,'F13':7,'F14':6,'F15':8,
    'F16':3,'F17':16,'F18':16
}

def get_color_toc(fecha_creacion, fecha_entrega, today):
    if pd.isna(fecha_entrega):
        return 'gris'
    # Negro solo si YA PASÓ la fecha (día siguiente en adelante)
    if today.date() > fecha_entrega.date():
        return 'negro'
    # Si no hay fecha de creación, usar solo la fecha de entrega
    if pd.isna(fecha_creacion):
        return 'rojo'  # sin info de buffer, marcar como urgente
    duracion = (fecha_entrega - fecha_creacion).days
    if duracion <= 0:
        return 'rojo'  # orden con duración 0 o negativa pero no vencida = rojo
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

def process_excel(file):
    df = pd.read_excel(file, header=0)
    df['fecha_entrega'] = pd.to_datetime(df.iloc[:, 9], errors='coerce')
    df['fecha_creacion'] = pd.to_datetime(df.iloc[:, 20], format='%d-%m-%Y %I:%M %p', errors='coerce')
    df['etapa'] = df.iloc[:, 7].astype(str).str.strip()
    df['mts'] = pd.to_numeric(df.iloc[:, 8], errors='coerce').fillna(0)
    df['familia'] = df.iloc[:, 6].astype(str).str.strip()

    # Etapas que indican que la orden ya salió de impresión y está en rebobinado
    ETAPAS_REBOBINADO = {'RM4 - Reboninado', 'En cola Rebobinadora 1', 'En cola Rebobinadora',
                         'Rebobinando', 'Cola rebobinado'}
    # Etapas que indican troquelado
    ETAPAS_TROQUELADO = {'En cola Troqueladora 4', 'En cola Troqueladora', 'Troquelando'}

    def asignar_maquina(row):
        etapa = str(row.iloc[7]).strip()
        proceso = str(row.iloc[16]).strip()
        primera = proceso.split(',')[0].strip()
        # Si está en rebobinado → asignar a Rebobinadora
        if etapa in ETAPAS_REBOBINADO or 'RM4' in etapa.upper() or 'REBOB' in etapa.upper():
            return 'Rebobinadora'
        # Si está en cola de troqueladora → asignar a Troqueladora Rotativa
        if etapa in ETAPAS_TROQUELADO or 'TROQUELAD' in etapa.upper():
            return 'Troqueladora Rotativa 1'
        # Default: primera máquina del proceso
        return primera

    df['maquina'] = df.apply(asignar_maquina, axis=1)

    now_bogota = datetime.now(BOGOTA)
    today = pd.Timestamp(now_bogota.date())

    if today.weekday() < 5:
        lun = today - pd.Timedelta(days=today.weekday())
    else:
        lun = today + pd.Timedelta(days=(7 - today.weekday()))
    vie = lun + pd.Timedelta(days=4)

    incumplidas_df = df[df['fecha_entrega'] < today].copy()
    maquinas = sorted(df['maquina'].dropna().unique().tolist())

    # Incluir dias anteriores que tengan ordenes pendientes
    dias_semana = pd.date_range(lun, vie, freq='B')
    # Buscar dias previos con ordenes (desde 2 semanas atras)
    dos_semanas_atras = lun - pd.Timedelta(weeks=2)
    dias_prev_con_ordenes = sorted(set(
        df[(df['fecha_entrega'] >= dos_semanas_atras) & (df['fecha_entrega'] < lun) & df['fecha_entrega'].notna()]
        ['fecha_entrega'].dt.strftime('%Y-%m-%d').tolist()
    ))
    # Combinar: dias previos + semana actual
    todos_dias = [pd.Timestamp(d) for d in dias_prev_con_ordenes] + list(dias_semana)
    dias_str = [d.strftime('%Y-%m-%d') for d in todos_dias]
    dias_label = [d.strftime('%d/%m') for d in todos_dias]
    nombres_map = {0:'Lunes',1:'Martes',2:'Miércoles',3:'Jueves',4:'Viernes',5:'Sábado',6:'Domingo'}
    dias_nombre = [nombres_map[d.weekday()] for d in todos_dias]

    pivot = {}
    totales_dia = {d: 0 for d in dias_str}
    for m in maquinas:
        pivot[m] = {}
        for d in dias_str:
            n = len(df[(df['maquina'] == m) & (df['fecha_entrega'].dt.strftime('%Y-%m-%d') == d)])
            pivot[m][d] = n
            totales_dia[d] += n

    # Total solo de la semana actual
    total_semana = sum(totales_dia.get(d.strftime('%Y-%m-%d'), 0) for d in dias_semana)

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
        fc = row['fecha_creacion']
        color_toc = get_color_toc(fc, fe, today)
        # Fecha estándar = fecha_creacion + promesa_dias de la familia
        fam_ord = str(row['familia']).strip()
        dias_p = FAMILIAS_PROMESA.get(fam_ord, 0)
        fecha_std = ''
        if pd.notna(fc) and dias_p > 0:
            from datetime import timedelta as _td
            _f = fc; _d = 0
            while _d < dias_p:
                _f += _td(days=1)
                if _f.weekday() != 6: _d += 1
            fecha_std = _f.strftime('%Y-%m-%d')
        todas_ordenes.append({
            'fecha': fe.strftime('%Y-%m-%d'),
            'fecha_entrega': fe.strftime('%d/%m/%Y'),
            'op': str(row.iloc[3]),
            'cliente': str(row.iloc[0]),
            'referencia': str(row.iloc[1]),
            'maquina': str(row['maquina']),
            'etapa': str(row.iloc[7]).strip(),
            'tipo': get_tipo(row.iloc[1]),
            'mts': float(row['mts']),
            'familia': fam_ord,
            'color_toc': color_toc,
            'fecha_std': fecha_std,
            'promesa_dias': dias_p,
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
    # === CUELLOS DE BOTELLA ===
    carga_maq = {m: {'mts': 0, 'ordenes': []} for m in MAQUINAS_CAP}
    trot_counter = [0]

    REB_NAMES = ['Rebobinadora 1','Rebobinadora 2','Rebobinadora 3','Rebobinadora (T) 4','Rebobinadora KM']
    reb_counter = [0]

    # Etapas válidas para impresoras (aún en proceso de impresión)
    ETAPAS_IMPRESION = {'Preparacion', 'En cola impresión NP1', 'Impresion',
                        'En cola impresion NP1', 'En cola impresión NP2', 'En cola impresión Kromia'}

    for _, row in df.iterrows():
        proceso = str(row.iloc[16]).strip()
        etapa_ord = str(row.iloc[7]).strip()
        mts_ord = float(row['mts'])
        fe_ord = row['fecha_entrega']
        op_ord = str(row.iloc[3])
        cli_ord = str(row.iloc[0])
        color_ord = get_color_toc(row['fecha_creacion'], fe_ord, today)
        maquinas_ord = get_maquinas_reales(proceso)

        # Si la orden ya salió de impresión, no contarla en las impresoras
        p_upper = proceso.upper()
        en_impresora = any(m in ['NP1','NP2','Kromia'] for m in maquinas_ord)
        if en_impresora and etapa_ord not in ETAPAS_IMPRESION:
            # Quitar impresoras de la lista, solo dejar rebobinadoras/troqueladoras
            maquinas_ord = [m for m in maquinas_ord if m not in ['NP1','NP2','Kromia']]

        # Resolver marcadores de balanceo
        maquinas_final = []
        for m in maquinas_ord:
            if m == 'REBOBINADORA_BALANCEAR':
                # Balancear en las 5 rebobinadoras por carga acumulada
                min_maq = min(REB_NAMES, key=lambda x: carga_maq[x]['mts'])
                maquinas_final.append(min_maq)
            else:
                maquinas_final.append(m)

        # Troqueladoras rotativas: balancear en 4 por carga acumulada
        if 'TROQUELADORA ROTATIVA' in proceso.upper():
            min_trot = min(TROT_NAMES, key=lambda x: carga_maq[x]['mts'])
            maquinas_final.append(min_trot)

        for m in maquinas_final:
            if m in carga_maq:
                carga_maq[m]['mts'] += mts_ord
                carga_maq[m]['ordenes'].append({
                    'op': op_ord,
                    'cliente': cli_ord,
                    'mts': mts_ord,
                    'fecha': fe_ord.strftime('%d/%m/%Y') if pd.notna(fe_ord) else '',
                    'color': color_ord,
                    'tipo': get_tipo(row.iloc[1]),
                    'etapa': etapa_ord,
                })

    def sumar_dias_lab(fecha_ini, dias):
        from datetime import timedelta
        f = fecha_ini
        d = 0
        while d < int(dias):
            f += timedelta(days=1)
            if f.weekday() != 6:
                d += 1
        return f

    cuellos = {}
    for m, info in carga_maq.items():
        cap = MAQUINAS_CAP[m]
        mts_total = info['mts']
        dias_trabajo = round(mts_total / cap, 1) if cap > 0 else 0
        pct_cap = round(mts_total / (cap * 20) * 100, 1)
        fecha_prom = sumar_dias_lab(today, dias_trabajo)

        # Carga acumulada por fecha para esta maquina
        carga_por_fecha = {}
        ordenes_sorted = sorted(info['ordenes'], key=lambda x: x['fecha'])
        for o in ordenes_sorted:
            f = o['fecha']  # dd/mm/yyyy
            if f not in carga_por_fecha:
                carga_por_fecha[f] = {'mts': 0, 'ordenes': 0}
            carga_por_fecha[f]['mts'] += o['mts']
            carga_por_fecha[f]['ordenes'] += 1

        cuellos[m] = {
            'capacidad_dia': cap,
            'mts_total': round(mts_total),
            'dias_trabajo': dias_trabajo,
            'pct_cap': pct_cap,
            'ordenes': ordenes_sorted,
            'es_cuello': dias_trabajo > 10,
            'fecha_prometida': fecha_prom.strftime('%d/%m/%Y'),
            'carga_por_fecha': carga_por_fecha,
        }

    # Datos simulación tiempos estándar
    ETAPAS_IMP_STD = {'Preparacion','En cola impresión NP1','Impresion','En cola impresion NP1'}
    std_data = {m: {} for m in maquinas_rrc}
    for o in todas_ordenes:
        if o['maquina'] in maquinas_rrc and o.get('fecha_std') and o['etapa'] in ETAPAS_IMP_STD:
            f = o['fecha_std']
            std_data[o['maquina']][f] = std_data[o['maquina']].get(f, 0) + o['mts']
    fechas_std = sorted(set(f for m in maquinas_rrc for f in std_data[m].keys()))

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

    # Órdenes próximas: desde hoy hasta 7 días
    # Todas las ordenes para el Tambor General
    limite_urgente = today + pd.Timedelta(days=7)
    urgentes = []
    todas_tambor = []
    for _, row in df.sort_values('fecha_entrega').iterrows():
        fe = row['fecha_entrega']
        fc = row['fecha_creacion']
        color_toc = get_color_toc(fc, fe, today)
        etapa_str = str(row.iloc[7]).strip()
        fc_ord = row['fecha_creacion']
        if pd.notna(fe) and pd.notna(fc_ord):
            dur = (fe - fc_ord).days
            pct_buf = round((today - fc_ord).days / dur * 100, 1) if dur > 0 else 0
        else:
            pct_buf = 0
        ord_data = {
            'op': str(row.iloc[3]),
            'cliente': str(row.iloc[0]),
            'referencia': str(row.iloc[1]),
            'maquina': str(row['maquina']),
            'etapa': etapa_str,
            'tipo': get_tipo(row.iloc[1]),
            'fecha_entrega': fe.strftime('%d/%m/%Y') if pd.notna(fe) else '',
            'fecha_entrega_raw': fe.strftime('%Y-%m-%d') if pd.notna(fe) else '',
            'color_toc': color_toc,
            'mts': float(row['mts']),
            'pct_buffer': pct_buf,
            'en_impresion': etapa_str in {'Preparacion','En cola impresión NP1','Impresion','En cola impresion NP1'},
        }
        todas_tambor.append(ord_data)
        if pd.notna(fe) and fe <= today + pd.Timedelta(days=7):
            urgentes.append(ord_data)

    # === DIAGNÓSTICO DE ATRASO ===
    ETAPAS_IMP_SET = {'Preparacion','En cola impresión NP1','Impresion','En cola impresion NP1',
                      'En cola impresión NP2','En cola impresión Kromia'}
    horas_nom = 22.75; efic_nom = 0.50; vel_nom = 35
    mins_turno_nom = horas_nom * 60 / 3
    mins_prod_nom  = mins_turno_nom * efic_nom
    cap_turno_nom  = round(mins_prod_nom * vel_nom)

    diagnostico = {}
    for mq in ['Nilpeter 1','Nilpeter 2','Kromia']:
        grp = df[(df['maquina']==mq) & (df['etapa'].isin(ETAPAS_IMP_SET))]
        atras = grp[grp['fecha_entrega'] < today]
        pend  = grp[grp['fecha_entrega'] >= today]
        mts_a = round(float(atras['mts'].sum()))
        mts_p = round(float(pend['mts'].sum()))
        mts_t = mts_a + mts_p
        turnos_atras = round(mts_a / cap_turno_nom, 1) if cap_turno_nom > 0 else 0
        planes = []
        for dias_rec in [3, 5, 7, 10]:
            t_disp = dias_rec * 3
            mpt = round(mts_t / t_disp) if t_disp > 0 else 0
            vr  = round(mpt / mins_prod_nom, 1) if mins_prod_nom > 0 else vel_nom
            er  = round(mpt / (vel_nom * mins_turno_nom) * 100, 1) if vel_nom * mins_turno_nom > 0 else 0
            vc  = round(min(vel_nom * 1.2, vr), 1)
            ec  = round(mpt / (vc * mins_turno_nom) * 100, 1) if vc * mins_turno_nom > 0 else 0
            planes.append({'dias': dias_rec, 'turnos': t_disp, 'mts_por_turno': mpt,
                           'vel_req': vr, 'efic_req': er, 'vel_comb': vc, 'efic_comb': ec})
        diagnostico[mq] = {
            'mts_atrasados': mts_a, 'n_atrasadas': len(atras),
            'mts_pendientes': mts_p, 'n_pendientes': len(pend),
            'mts_total': mts_t, 'turnos_atrasados': turnos_atras,
            'dias_atrasados': round(turnos_atras / 3, 1),
            'cap_turno': cap_turno_nom, 'planes': planes,
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
        'urgentes': urgentes,
        'urgentes_total': len(urgentes),
        'todas_tambor': todas_tambor,
        'inc_etapas': inc_etapas,
        'inc_detalle': inc_detalle,
        'etapas_unicas': etapas_unicas,
        'colores_resumen': {
            'azul': sum(1 for o in todas_ordenes if o['color_toc']=='azul'),
            'verde': sum(1 for o in todas_ordenes if o['color_toc']=='verde'),
            'amarillo': sum(1 for o in todas_ordenes if o['color_toc']=='amarillo'),
            'rojo': sum(1 for o in todas_ordenes if o['color_toc']=='rojo'),
            'negro': sum(1 for o in todas_ordenes if o['color_toc']=='negro'),
        },
        'dia_total': len(df[df['fecha_entrega'].dt.date == today.date()]),
        'dia_por_maquina': {},
        'fechas_disponibles': fechas_disponibles,
        'todas_ordenes': todas_ordenes,
        'maquinas_rrc': maquinas_rrc,
        'std_data': std_data,
        'fechas_std': fechas_std,
        'diagnostico': diagnostico,
        'cuellos': cuellos,
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

@app.route('/simulacion_tiempos')
def simulacion_tiempos_view():
    data = None
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, encoding='utf-8') as f:
            data = json.load(f)
    return render_template('simulacion_tiempos.html', data=data)

@app.route('/capacidad')
def capacidad_view():
    data = None
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, encoding='utf-8') as f:
            data = json.load(f)
    return render_template('capacidad.html', data=data)

@app.route('/tambor')
def tambor_view():
    data = None
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, encoding='utf-8') as f:
            data = json.load(f)
    return render_template('tambor.html', data=data)

@app.route('/cuellos')
def cuellos_view():
    data = None
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, encoding='utf-8') as f:
            data = json.load(f)
    return render_template('cuellos.html', data=data)

@app.route('/wip')
def wip_view():
    data = None
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, encoding='utf-8') as f:
            data = json.load(f)
    wip_data = None
    WIP_FILE = os.path.join(UPLOAD_FOLDER, 'wip.json')
    if os.path.exists(WIP_FILE):
        with open(WIP_FILE, encoding='utf-8') as f:
            wip_data = json.load(f)
    return render_template('wip.html', data=data, wip=wip_data)

@app.route('/pareto')
def pareto_view():
    wip_data = None
    WIP_FILE = os.path.join(UPLOAD_FOLDER, 'wip.json')
    if os.path.exists(WIP_FILE):
        with open(WIP_FILE, encoding='utf-8') as f:
            wip_data = json.load(f)
    return render_template('pareto.html', wip=wip_data)

@app.route('/upload_wip', methods=['POST'])
def upload_wip():
    if 'file' not in request.files:
        return jsonify({'error': 'No se recibio archivo'}), 400
    file = request.files['file']
    if not file.filename.endswith(('.xlsx', '.xls', '.xlsm')):
        return jsonify({'error': 'Solo se aceptan archivos Excel'}), 400
    try:
        import pytz
        from datetime import datetime
        now = datetime.now(pytz.timezone('America/Bogota'))
        file_bytes = file.read()
        import io

        # Hoja WIP — header=0 tiene los encabezados en fila 1, datos desde fila 2
        df_wip = pd.read_excel(io.BytesIO(file_bytes), sheet_name='Wip', header=0)
        df_wip.columns = ['area','ordenes_cola','tiempo_espera','acumulacion_alta','causa']
        df_wip = df_wip.dropna(subset=['area'])
        filas = []
        for _, row in df_wip.iterrows():
            filas.append({
                'area': str(row['area']).strip(),
                'ordenes_cola': int(row['ordenes_cola']) if pd.notna(row['ordenes_cola']) else 0,
                'tiempo_espera': float(row['tiempo_espera']) if pd.notna(row['tiempo_espera']) else 0,
                'acumulacion_alta': str(row['acumulacion_alta']).strip() if pd.notna(row['acumulacion_alta']) else 'N',
                'causa': str(row['causa']).strip() if pd.notna(row['causa']) else '',
            })

        # Hoja Pareto Problemas
        df_pareto = pd.read_excel(io.BytesIO(file_bytes), sheet_name='Pareto Problemas', header=0)
        col_problema = df_pareto.columns[-1]  # última columna = Problema
        df_pareto = df_pareto.dropna(subset=[col_problema])
        pareto_ordenes = []
        for _, row in df_pareto.iterrows():
            fe = row.iloc[9]  # Fecha de Entrega
            pareto_ordenes.append({
                'cliente': str(row.iloc[0]).strip(),
                'referencia': str(row.iloc[1]).strip()[:60],
                'op': str(row.iloc[3]).strip(),
                'etiquetas': int(row.iloc[4]) if pd.notna(row.iloc[4]) else 0,
                'mts': float(row.iloc[8]) if pd.notna(row.iloc[8]) else 0,
                'fecha_entrega': pd.to_datetime(fe, errors='coerce').strftime('%d/%m/%Y') if pd.notna(fe) else '',
                'proceso': str(row.iloc[17]).strip() if pd.notna(row.iloc[17]) else '',
                'problema': str(row[col_problema]).strip(),
            })

        # Conteo Pareto por problema
        from collections import Counter
        conteo = Counter(o['problema'] for o in pareto_ordenes)
        total_prob = sum(conteo.values())
        pareto_items = []
        acum = 0
        for prob, cnt in sorted(conteo.items(), key=lambda x: -x[1]):
            pct = round(cnt / total_prob * 100, 1) if total_prob > 0 else 0
            acum += pct
            pareto_items.append({'problema': prob, 'cantidad': cnt, 'pct': pct, 'acum': round(acum, 1)})

        wip_result = {
            'updated_at': now.strftime('%d/%m/%Y %H:%M'),
            'filas': filas,
            'total_ordenes': sum(f['ordenes_cola'] for f in filas),
            'areas_criticas': sum(1 for f in filas if f['acumulacion_alta'] == 'S'),
            'pareto_items': pareto_items,
            'pareto_ordenes': pareto_ordenes,
            'pareto_total': len(pareto_ordenes),
        }
        WIP_FILE = os.path.join(UPLOAD_FOLDER, 'wip.json')
        with open(WIP_FILE, 'w', encoding='utf-8') as f:
            json.dump(wip_result, f, ensure_ascii=False)
        return jsonify({'ok': True, 'updated_at': wip_result['updated_at']})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/capacidad2')
def capacidad2_view():
    data = None
    if os.path.exists(DATA_FILE):
        with open(DATA_FILE, encoding='utf-8') as f:
            data = json.load(f)
    lib_data = None
    LIB_FILE = os.path.join(UPLOAD_FOLDER, 'liberacion.json')
    if os.path.exists(LIB_FILE):
        with open(LIB_FILE, encoding='utf-8') as f:
            lib_data = json.load(f)
    return render_template('capacidad2.html', data=data, lib=lib_data)

@app.route('/upload_liberacion', methods=['POST'])
def upload_liberacion():
    if 'file' not in request.files:
        return jsonify({'error': 'No se recibio archivo'}), 400
    file = request.files['file']
    if not file.filename.endswith(('.xlsx','.xls','.xlsm')):
        return jsonify({'error': 'Solo Excel'}), 400
    try:
        from datetime import timedelta
        import pytz
        df = pd.read_excel(file, header=0)
        df.columns = ['op','fecha_lib','cliente','descripcion','fecha_entrega','mts','centro_proceso','proceso']
        df['fecha_lib'] = pd.to_datetime(df['fecha_lib'], dayfirst=True, errors='coerce')
        df['fecha_entrega'] = pd.to_datetime(df['fecha_entrega'], dayfirst=True, errors='coerce')
        df['mts'] = pd.to_numeric(df['mts'], errors='coerce').fillna(0)

        def sumar_dias_hab(fecha, dias):
            if pd.isna(fecha): return None
            f = fecha
            d = 0
            while d < dias:
                f += timedelta(days=1)
                if f.weekday() != 6: d += 1
            return f

        df['fecha_objetivo'] = df['fecha_lib'].apply(lambda x: sumar_dias_hab(x, 2))
        df['maquina'] = df['centro_proceso'].astype(str).str.split(',').str[0].str.strip()

        MAQUINAS_RRC = ['Nilpeter 1', 'Nilpeter 2', 'Kromia']
        ordenes = []
        for _, row in df.iterrows():
            fo = row['fecha_objetivo']
            ordenes.append({
                'op': str(row['op']),
                'cliente': str(row['cliente']),
                'maquina': str(row['maquina']),
                'mts': float(row['mts']),
                'fecha_lib': row['fecha_lib'].strftime('%Y-%m-%d') if pd.notna(row['fecha_lib']) else '',
                'fecha_objetivo': fo.strftime('%Y-%m-%d') if fo else '',
                'fecha_entrega': row['fecha_entrega'].strftime('%d/%m/%Y') if pd.notna(row['fecha_entrega']) else '',
                'referencia': str(row['descripcion']).strip()[:80],
                'proceso': str(row['centro_proceso']),
            })

        # Metros por maquina por fecha_objetivo
        cap_data = {}
        for m in MAQUINAS_RRC:
            grp = [o for o in ordenes if o['maquina'] == m]
            por_fecha = {}
            for o in grp:
                f = o['fecha_objetivo']
                if f:
                    por_fecha[f] = por_fecha.get(f, 0) + o['mts']
            cap_data[m] = {k: round(v, 1) for k, v in por_fecha.items()}

        fechas_rrc = sorted(set(f for m in MAQUINAS_RRC for f in cap_data[m].keys()))

        now = datetime.now(pytz.timezone('America/Bogota'))
        lib_result = {
            'updated_at': now.strftime('%d/%m/%Y %H:%M'),
            'ordenes': ordenes,
            'cap_data': cap_data,
            'fechas_rrc': fechas_rrc,
            'maquinas_rrc': MAQUINAS_RRC,
        }
        LIB_FILE = os.path.join(UPLOAD_FOLDER, 'liberacion.json')
        with open(LIB_FILE, 'w', encoding='utf-8') as f:
            json.dump(lib_result, f, ensure_ascii=False)
        return jsonify({'ok': True, 'updated_at': lib_result['updated_at']})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

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
