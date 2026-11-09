"""Client-side local-edge fallback for immutable versioned demo assets."""

from .common import Error, request

ASSETS = {
    "app-v1.txt": "Synthetic static asset, version one.",
    "app-v2.txt": "Synthetic static asset, version two.",
}


def _fetch(base, asset, label, deadline, cancel):
    value = request(
        base + "/assets/" + asset, "", timeout=1.5, deadline=deadline, cancel=cancel
    )
    if value.get("asset") != asset or value.get("content") != ASSETS[asset]:
        raise OSError(label + " returned another immutable asset")
    return value


def fetch_asset(config, asset, *, deadline=None, cancel=None):
    if asset not in ASSETS:
        raise ValueError("known versioned asset required")
    edge = config.get("edge")
    if edge:
        try:
            value = _fetch(edge, asset, "edge", deadline, cancel)
            return {**value, "asset_source": "edge"}
        except Error as exc:
            if exc.status < 500:
                raise
        except OSError:
            pass
    value = _fetch(config["entry"], asset, "origin", deadline, cancel)
    return {**value, "asset_source": "origin_fallback" if edge else "origin"}
