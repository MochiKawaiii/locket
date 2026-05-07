from flask import current_app, jsonify, render_template, request

from . import bp


def _no_accounts_response():
    return jsonify({
        "success": False,
        "msg": "Chưa có tài khoản Locket nào. Admin hãy thêm qua /admin.",
    }), 503


@bp.route("/")
def index():
    return render_template("index.html")


@bp.route("/api/get-user-info", methods=["POST"])
def get_user_info():
    rotator = current_app.rotator
    qm = current_app.queue_manager
    if rotator is None or rotator.size() == 0:
        return _no_accounts_response()

    data = request.json or {}
    username = data.get("username")
    if not username:
        return jsonify({"success": False, "msg": "Username is required"}), 400

    try:
        print(f"Looking up user: {username}")
        account_info = qm.call_round_robin("getUserByUsername", username)

        if not account_info or "result" not in account_info:
            return jsonify({"success": False, "msg": "User not found or API error"}), 404

        user_data = account_info.get("result", {}).get("data")
        if not user_data:
            return jsonify({"success": False, "msg": "User data not found"}), 404

        return jsonify({
            "success": True,
            "data": {
                "uid": user_data.get("uid"),
                "username": user_data.get("username"),
                "first_name": user_data.get("first_name", ""),
                "last_name": user_data.get("last_name", ""),
                "profile_picture_url": user_data.get("profile_picture_url", ""),
            },
        })

    except Exception as e:
        print(f"Error in get user info: {e}")
        return jsonify({"success": False, "msg": f"An error occurred: {str(e)}"}), 500


@bp.route("/api/restore", methods=["POST"])
def restore_purchase():
    """Add a request to the queue. Returns client_id for polling."""
    rotator = current_app.rotator
    qm = current_app.queue_manager
    if rotator is None or rotator.size() == 0:
        return _no_accounts_response()

    data = request.json or {}
    username = data.get("username")
    if not username:
        return jsonify({"success": False, "msg": "Username is required"}), 400

    try:
        client_id = qm.add_to_queue(username)
        if client_id is None:
            return jsonify({"success": False, "msg": "Queue is full, please try again later."}), 503

        status = qm.get_status(client_id)
        return jsonify({
            "success": True,
            "client_id": client_id,
            "position": status["position"],
            "total_queue": status["total_queue"],
            "estimated_time": status["estimated_time"],
        })
    except Exception as e:
        print(f"Error adding to queue: {e}")
        return jsonify({"success": False, "msg": f"An error occurred: {str(e)}"}), 500


@bp.route("/api/queue/global-status", methods=["GET"])
def global_queue_status():
    """Aggregate queue stats — no client_id required."""
    return jsonify({"success": True, **current_app.queue_manager.get_global_status()})


@bp.route("/api/queue/status", methods=["POST"])
def queue_status():
    """Per-client polling endpoint. Returns success even on `not_found` so
    the frontend can recover instead of treating it as fatal."""
    data = request.json or {}
    client_id = data.get("client_id")
    if not client_id:
        return jsonify({"success": False, "msg": "client_id is required"}), 400
    return jsonify({"success": True, **current_app.queue_manager.get_status(client_id)})
