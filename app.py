from flask import Flask, jsonify, request, render_template, Response
import json, uuid, time, threading
from datetime import datetime

app = Flask(__name__)

# ── In-memory state ──────────────────────────────────────────────────────────
state = {
    "orders": [],        # list of order dicts
    "catalog": [],       # uploaded Excel catalog
    "updated_at": None,
}
state_lock = threading.Lock()
subscribers = []         # SSE clients
subs_lock   = threading.Lock()

# ── SSE broadcast ────────────────────────────────────────────────────────────
def broadcast(event="update"):
    with state_lock:
        data = json.dumps({"orders": state["orders"], "updated_at": state["updated_at"]})
    msg = f"event: {event}\ndata: {data}\n\n"
    with subs_lock:
        dead = []
        for q in subscribers:
            try:
                q.append(msg)
            except Exception:
                dead.append(q)
        for q in dead:
            subscribers.remove(q)

# ── Routes ───────────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template("index.html")

@app.route("/stream")
def stream():
    """SSE endpoint — pushes updates to all connected clients."""
    q = []
    with subs_lock:
        subscribers.append(q)
    # Send current state immediately on connect
    with state_lock:
        initial = json.dumps({"orders": state["orders"], "updated_at": state["updated_at"]})
    q.append(f"event: init\ndata: {initial}\n\n")

    def generate():
        try:
            while True:
                if q:
                    yield q.pop(0)
                else:
                    yield ": ping\n\n"
                    time.sleep(2)
        except GeneratorExit:
            with subs_lock:
                if q in subscribers:
                    subscribers.remove(q)
    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.route("/api/orders", methods=["GET"])
def get_orders():
    with state_lock:
        return jsonify({"orders": state["orders"], "updated_at": state["updated_at"]})

@app.route("/api/orders", methods=["POST"])
def create_order():
    data = request.json
    now  = datetime.utcnow().isoformat() + "Z"
    # Compute turno from creation time — ajuste a GMT-5 (Colombia)
    from datetime import datetime as dt2, timezone, timedelta
    tz_col = timezone(timedelta(hours=-5))
    hour = dt2.fromisoformat(now.replace("Z","")).replace(tzinfo=timezone.utc).astimezone(tz_col).hour
    if 6 <= hour < 14:    turno = "T1 (06:00-14:00)"
    elif 14 <= hour < 22: turno = "T2 (14:00-22:00)"
    else:                 turno = "T3 (22:00-06:00)"

    order = {
        "id":                str(uuid.uuid4()),
        "ordenId":           data.get("ordenId", ""),
        "producto":          data.get("producto", ""),
        "cliente":           data.get("cliente", ""),
        "maquina":           data.get("maquina", ""),
        "operario":          data.get("operario", ""),
        "turno":             turno,
        "cantidad":          data.get("cantidad", 0),
        "velocidadObjetivo": data.get("velocidadObjetivo", 0),
        "velocidadActual":   data.get("velocidadActual", 0),
        "notas":             data.get("notas", ""),
        "status":            data.get("status", "setup"),
        "createdAt":         now,
        "ajustes":           [],
        "history":           [{"status": data.get("status", "setup"), "at": now}],
    }
    with state_lock:
        state["orders"].append(order)
        state["updated_at"] = now
    broadcast()
    return jsonify(order), 201

@app.route("/api/orders/<oid>/status", methods=["PATCH"])
def update_status(oid):
    new_status = request.json.get("status")
    now = datetime.utcnow().isoformat() + "Z"
    with state_lock:
        for o in state["orders"]:
            if o["id"] == oid:
                o["status"]  = new_status
                o["history"] = o.get("history", []) + [{"status": new_status, "at": now}]
                if new_status == "finalizada":
                    o["finalizadaAt"] = now
        state["updated_at"] = now
    broadcast()
    return jsonify({"ok": True})

@app.route("/api/orders/<oid>/velocidad", methods=["PATCH"])
def update_velocidad(oid):
    vel = request.json.get("velocidadActual", 0)
    now = datetime.utcnow().isoformat() + "Z"
    with state_lock:
        for o in state["orders"]:
            if o["id"] == oid:
                o["velocidadActual"] = vel
        state["updated_at"] = now
    broadcast()
    return jsonify({"ok": True})

@app.route("/api/orders/<oid>/ajuste", methods=["POST"])
def add_ajuste(oid):
    data = request.json
    now  = datetime.utcnow().isoformat() + "Z"
    ajuste = {"descripcion": data.get("descripcion",""), "duracion": data.get("duracion",""), "at": now}
    with state_lock:
        for o in state["orders"]:
            if o["id"] == oid:
                o.setdefault("ajustes", []).append(ajuste)
        state["updated_at"] = now
    broadcast()
    return jsonify({"ok": True})

@app.route("/api/catalog", methods=["POST"])
def upload_catalog():
    rows = request.json.get("rows", [])
    with state_lock:
        state["catalog"] = rows
    return jsonify({"ok": True, "count": len(rows)})

@app.route("/api/catalog", methods=["GET"])
def get_catalog():
    with state_lock:
        return jsonify({"catalog": state["catalog"]})

if __name__ == "__main__":
    app.run(debug=True, threaded=True, host="0.0.0.0", port=5000)
