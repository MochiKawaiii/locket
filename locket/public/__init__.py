from flask import Blueprint

bp = Blueprint("public", __name__)

# Late import keeps blueprint object instantiable without circulars.
from . import routes  # noqa: E402,F401
