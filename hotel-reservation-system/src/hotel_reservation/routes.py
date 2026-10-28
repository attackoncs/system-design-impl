"""Explicit public route contracts shared by the gateway and shard API."""

from dataclasses import dataclass
from urllib.parse import parse_qs, urlsplit

from .common import Error, booking_hotel, identifier


@dataclass(frozen=True)
class Route:
    operation: str
    required: str
    hotel: str | None = None
    room: str | None = None
    booking: str | None = None


def parameters(query):
    values = parse_qs(
        query, keep_blank_values=True, strict_parsing=True, max_num_fields=16
    )
    if any(len(items) != 1 for items in values.values()):
        raise ValueError("repeated query parameter")
    return {key: items[0] for key, items in values.items()}


def public_route(path, method):
    if not isinstance(path, str) or len(path) > 4096:
        raise ValueError("invalid route")
    parsed = urlsplit(path)
    if (
        parsed.scheme
        or parsed.netloc
        or parsed.fragment
        or parsed.path.endswith("/")
        or "%" in parsed.path
    ):
        raise ValueError("noncanonical request target")
    params = parameters(parsed.query)
    parts = parsed.path.split("/")
    if len(parts) < 3 or parts[:2] != ["", "v1"]:
        raise Error(404, "unknown public route")
    route = None
    allowed = set()
    if parts[2] == "hotels":
        if len(parts) == 3 and method == "POST":
            route = Route("create_hotel", "admin")
        elif len(parts) >= 4:
            hotel = identifier(parts[3])
            if len(parts) == 4 and method in ("GET", "PUT", "DELETE"):
                route = Route("hotel", "user" if method == "GET" else "admin", hotel)
            elif len(parts) == 5 and parts[4] == "availability" and method == "GET":
                route, allowed = (
                    Route("hotel_availability", "user", hotel),
                    {"start", "end", "quantity"},
                )
            elif parts[4:5] == ["rooms"]:
                if len(parts) == 5 and method == "POST":
                    route = Route("create_room", "admin", hotel)
                elif len(parts) >= 6:
                    room = identifier(parts[5])
                    if len(parts) == 6 and method in ("GET", "PUT", "DELETE"):
                        route = Route(
                            "room", "user" if method == "GET" else "admin", hotel, room
                        )
                    elif len(parts) == 7:
                        suffix = parts[6]
                        if suffix == "calendar" and method == "PUT":
                            route = Route("calendar", "admin", hotel, room)
                        elif suffix == "availability" and method == "GET":
                            route, allowed = (
                                Route("availability", "user", hotel, room),
                                {"start", "end"},
                            )
                        elif suffix == "calendar-policy" and method in (
                            "GET",
                            "PUT",
                            "DELETE",
                        ):
                            route = Route("calendar_policy", "admin", hotel, room)
    elif parts[2] == "reservations":
        if len(parts) == 3 and method == "POST":
            route = Route("reserve", "guest")
        elif len(parts) == 3 and method == "GET":
            route, allowed = Route("history", "guest"), {"hotel", "after"}
        elif len(parts) == 4 and method in ("GET", "DELETE"):
            hotel = booking_hotel(parts[3])
            route = Route("booking", "user", hotel, booking=parts[3])
    if route is None:
        raise Error(404, "unknown public route or method")
    if set(params) - allowed:
        raise ValueError("unknown query parameter")
    if route.operation in ("availability", "hotel_availability") and not {
        "start",
        "end",
    } <= set(params):
        raise ValueError("availability requires start and end")
    if route.operation == "history" and "hotel" not in params:
        raise ValueError("history requires hotel")
    return route, params


def routing_hotel(route, body, params):
    if route.hotel is not None:
        return route.hotel
    if route.operation == "create_hotel":
        return identifier(body["id"])
    if route.operation == "reserve":
        return identifier(body["hotel"])
    if route.operation == "history":
        return identifier(params["hotel"])
    raise Error(404, "unroutable public operation")
