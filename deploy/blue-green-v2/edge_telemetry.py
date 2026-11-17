"""Apply only the Player access-log delta approved in Caddy 2.11.4 for V2.6.8."""

from copy import deepcopy

from release_state import ROLES, Stop, canonical, require


CLASSIFIER = {
    "handler": "map",
    "source": "{http.request.method} {http.request.uri.path}",
    "destinations": ["{player_route_class}"],
    "defaults": ["OTHER"],
    "mappings": [
        {"input": "POST /api/player/heartbeat", "outputs": ["PLAYER_HEARTBEAT"]},
        {"input": "GET /api/player/runtime-config", "outputs": ["PLAYER_RUNTIME_CONFIG"]},
        {"input": "POST /api/player/playback-events", "outputs": ["PLAYER_PLAYBACK_EVENT"]},
    ],
}
PROTECTED_LOG_FIELDS = ("error", "headers", "request", "resp_headers")
NEW_LOG_FIELDS = {"route_class", "slot", "upstream_duration_ms"}


def _log(key, value, *, early=False):
    result = {"handler": "log_append", "key": key, "value": value}
    if early:
        result["early"] = True
    return result


def _reserved(node):
    if isinstance(node, list):
        return any(_reserved(item) for item in node)
    if not isinstance(node, dict):
        return False
    return (
        node.get("handler") == "log_append" and node.get("key") in NEW_LOG_FIELDS
        or node.get("handler") == "map" and "{player_route_class}" in node.get("destinations", ())
        or any(_reserved(value) for value in node.values())
    )


def _children(route):
    handlers = route["handle"]
    require(len(handlers) == 1 and handlers[0]["handler"] == "subroute"
            and isinstance(handlers[0]["routes"], list), "EDGE_TELEMETRY_LAYOUT_MISMATCH")
    return handlers[0]["routes"]


def _insert(handlers, index, entry, remove):
    if remove:
        require(index < len(handlers) and canonical(handlers[index]) == canonical(entry), "EDGE_TELEMETRY_LAYOUT_MISMATCH")
        handlers.pop(index)
    else:
        handlers.insert(index, entry)


def _site(server, host, role, active_slot, preview, remove):
    sites = [route for route in server["routes"] if route.get("match") == [{"host": [host]}]]
    require(len(sites) == 1, "EDGE_TELEMETRY_LAYOUT_MISMATCH")
    routes = _children(sites[0])
    classification = _log("route_class", "{player_route_class}" if role == "api" else "OTHER", early=True)
    if preview:
        prefix = ([deepcopy(CLASSIFIER)] if role == "api" else []) + [classification]
        if remove:
            _insert(routes, 0, {"handle": prefix}, True)
        require(len(routes) == 2 and "match" in routes[0] and routes[0].get("group") == routes[1].get("group"),
                "EDGE_TELEMETRY_LAYOUT_MISMATCH")
        proxy_routes = _children(routes[0])
    else:
        if role == "api" and remove:
            _insert(routes, 0, {"handle": [CLASSIFIER]}, True)
        proxy_routes = routes

    # Caddy adapts the existing proxy logging and proxy into one unconditional
    # handler list. Inspect the original matched route and preserve that list.
    require(proxy_routes and set(proxy_routes[-1]) == {"handle"}, "EDGE_TELEMETRY_LAYOUT_MISMATCH")
    for matched in proxy_routes[:-1]:
        [metadata] = matched["handle"]
        require("match" in matched and metadata.get("handler") == "log_append"
                and metadata.get("key") == "endpoint" and metadata.get("early") is True,
                "EDGE_TELEMETRY_LAYOUT_MISMATCH")
    handlers = proxy_routes[-1]["handle"]
    if not preview and remove:
        _insert(handlers, 0, classification, True)
    require(handlers and handlers[0].get("handler") == "log_append"
            and handlers[0].get("key") == "route_generation"
            and handlers[-1].get("handler") == "reverse_proxy", "EDGE_TELEMETRY_LAYOUT_MISMATCH")
    if remove:
        _insert(handlers, 1, _log("slot", active_slot, early=True), True)
    upstream_log = _log("upstream", "{http.reverse_proxy.upstream.hostport}")
    positions = [index for index, handler in enumerate(handlers) if handler == upstream_log]
    require(len(positions) == 1, "EDGE_TELEMETRY_LAYOUT_MISMATCH")
    _insert(handlers, positions[0] + 1,
            _log("upstream_duration_ms", "{http.reverse_proxy.upstream.duration_ms}"), remove)
    if not remove:
        _insert(handlers, 1, _log("slot", active_slot, early=True), False)
        if preview:
            _insert(routes, 0, {"handle": prefix}, False)
        else:
            _insert(handlers, 0, classification, False)
            if role == "api":
                _insert(routes, 0, {"handle": [deepcopy(CLASSIFIER)]}, False)


def _transform(config, hosts, *, remove):
    # Local import keeps the config policy independent of the controller's
    # config-update and observer command entry points.
    from routectl import RouteConfig

    try:
        current = RouteConfig(config, hosts)
        changed = deepcopy(config)
        logs = changed["logging"]["logs"]
        encoder = logs["default"]["encoder"]
        require(set(logs) == {"default"} and encoder["format"] == "filter" and encoder["wrap"] == {"format": "json"}
                and all(encoder["fields"].get(field) == {"filter": "delete"} for field in PROTECTED_LOG_FIELDS),
                "EDGE_TELEMETRY_REDACTION_MISMATCH")
        require(remove or not _reserved(changed), "EDGE_TELEMETRY_ALREADY_PRESENT_OR_PARTIAL")
        servers = changed["apps"]["http"]["servers"]
        for name, preview in (("public", None), ("preview-blue", "BLUE"), ("preview-green", "GREEN")):
            for role in ROLES:
                _site(servers[name], current.hosts[role], role, preview or current.active[role],
                      preview is not None, remove)
        require(not remove or not _reserved(changed), "EDGE_TELEMETRY_LAYOUT_MISMATCH")
        desired = RouteConfig(changed, current.hosts)
        require((desired.generation, desired.active, desired.targets, desired.proxies)
                == (current.generation, current.active, current.targets, current.proxies),
                "EDGE_TELEMETRY_ROUTING_CHANGED")
        return changed
    except (KeyError, TypeError, ValueError, IndexError, AttributeError) as exc:
        raise Stop("EDGE_TELEMETRY_LAYOUT_MISMATCH") from exc


def telemetry_update(config, hosts=None):
    updated = _transform(config, hosts, remove=False)
    require(canonical(_transform(updated, hosts, remove=True)) == canonical(config), "EDGE_TELEMETRY_ROUND_TRIP_MISMATCH")
    return updated


def telemetry_rollback(config, hosts=None):
    previous = _transform(config, hosts, remove=True)
    require(canonical(_transform(previous, hosts, remove=False)) == canonical(config), "EDGE_TELEMETRY_ROUND_TRIP_MISMATCH")
    return previous


def telemetry_enabled(config, hosts=None):
    try:
        telemetry_rollback(config, hosts)
    except Stop:
        return False
    return True
