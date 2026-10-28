"""Client-side local-edge fallback for immutable versioned demo assets."""

from .common import Error, request

ASSETS = {
    "app-v1.txt": "Synthetic static asset, version one.",
    "app-v2.txt": "Synthetic static asset, version two.",
}


def fetch_asset(config, asset, *, deadline=None, cancel=None):
    if asset not in ASSETS:
        raise ValueError("known versioned asset required")
    edge = config.get("edge")
    if edge:
        try:
            value = request(
                edge + "/assets/" + asset,
                "",
                timeout=1.5,
                deadline=deadline,
                cancel=cancel,
            )
            if value.get("asset") != asset or value.get("content") != ASSETS[asset]:
                raise OSError("edge returned another immutable asset")
            return {**value, "asset_source": "edge"}
        except Error as exc:
            if exc.status < 500:
                raise
        except OSError:
            pass
    value = request(
        config["entry"] + "/assets/" + asset,
        "",
        timeout=1.5,
        deadline=deadline,
        cancel=cancel,
    )
    if value.get("asset") != asset or value.get("content") != ASSETS[asset]:
        raise OSError("origin returned another immutable asset")
    return {**value, "asset_source": "origin_fallback" if edge else "origin"}
