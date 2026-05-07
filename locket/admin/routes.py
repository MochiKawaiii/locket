import json

from flask import current_app, jsonify, redirect, render_template, request, session

from ..rotator import AccountRotator
from ..tokens import tokens_store
from . import bp
from .auth import admin_required, check_password, is_admin_logged_in


@bp.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "GET":
        if is_admin_logged_in():
            return redirect("/admin/")
        return render_template("admin_login.html", error=None)

    password = (request.form.get("password") or "").strip()
    if not check_password(password):
        return render_template("admin_login.html", error="Mật khẩu không đúng"), 401
    session.clear()
    session["admin"] = True
    return redirect("/admin/")


@bp.route("/logout", methods=["POST"])
def logout():
    session.clear()
    return redirect("/admin/login")


@bp.route("/")
@admin_required
def dashboard():
    return render_template("admin.html")


# ---- accounts ----


@bp.route("/api/accounts", methods=["GET"])
@admin_required
def accounts_list():
    rotator = current_app.rotator
    if rotator is None:
        return jsonify({"success": False, "error": "rotator not initialized"}), 500
    return jsonify({"success": True, "accounts": rotator.list_accounts()})


@bp.route("/api/accounts", methods=["POST"])
@admin_required
def accounts_add():
    rotator = current_app.rotator
    if rotator is None:
        return jsonify({"success": False, "error": "rotator not initialized"}), 500

    body = request.get_json(silent=True) or {}
    email = (body.get("email") or "").strip()
    password = body.get("password") or ""
    if not email or not password:
        return jsonify({"success": False, "error": "email và password bắt buộc"}), 400

    ok, err = rotator.test_login(email, password)
    if not ok:
        return jsonify({"success": False, "error": f"Login thất bại: {err}"}), 400

    slot_id = rotator.add(email, password)
    current_app.queue_manager.add_worker(slot_id)
    return jsonify({"success": True, "id": slot_id, "email": email})


@bp.route("/api/accounts/<slot_id>", methods=["DELETE"])
@admin_required
def accounts_remove(slot_id):
    rotator = current_app.rotator
    if rotator is None:
        return jsonify({"success": False, "error": "rotator not initialized"}), 500
    if not rotator.has(slot_id):
        return jsonify({"success": False, "error": "không tồn tại"}), 404
    if rotator.size() <= 1:
        return jsonify({"success": False, "error": "phải còn ít nhất 1 account"}), 400
    current_app.queue_manager.remove_worker(slot_id)
    rotator.remove(slot_id)
    return jsonify({"success": True})


@bp.route("/api/accounts/test", methods=["POST"])
@admin_required
def accounts_test():
    body = request.get_json(silent=True) or {}
    email = (body.get("email") or "").strip()
    password = body.get("password") or ""
    if not email or not password:
        return jsonify({"success": False, "error": "email và password bắt buộc"}), 400
    ok, err = AccountRotator.test_login(email, password)
    return jsonify({"success": ok, "error": err})


# ---- tokens ----


@bp.route("/api/tokens", methods=["GET"])
@admin_required
def tokens_list():
    return jsonify({"success": True, "tokens": tokens_store.list()})


@bp.route("/api/tokens", methods=["POST"])
@admin_required
def tokens_add():
    body = request.get_json(silent=True) or {}
    payload = body.get("payload")
    if payload is None:
        # Allow raw JSON in a "raw" string field (UI textarea convenience).
        raw = body.get("raw")
        if not raw:
            return jsonify({"success": False, "error": "thiếu payload"}), 400
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError as e:
            return jsonify({"success": False, "error": f"JSON không hợp lệ: {e}"}), 400
    try:
        tokens_store.add(payload)
    except (ValueError, OSError) as e:
        return jsonify({"success": False, "error": str(e)}), 400
    return jsonify({"success": True})


@bp.route("/api/tokens/<int:index>", methods=["DELETE"])
@admin_required
def tokens_remove(index):
    try:
        tokens_store.remove(index)
    except IndexError as e:
        return jsonify({"success": False, "error": str(e)}), 404
    return jsonify({"success": True})


# ---- queue ----


@bp.route("/api/queue", methods=["GET"])
@admin_required
def queue_snapshot():
    qm = current_app.queue_manager
    rotator = current_app.rotator
    snap = qm.admin_snapshot()
    worker_emails = {}
    for slot_id in list(qm.workers.keys()):
        try:
            worker_emails[slot_id] = rotator.email(slot_id) if rotator else "<no rotator>"
        except KeyError:
            worker_emails[slot_id] = "<removed>"
    return jsonify({"success": True, "workers": worker_emails, **snap})
