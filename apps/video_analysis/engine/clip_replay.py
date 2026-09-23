"""Top-down observations with explicit uncertainty about elevated balls."""

from .clip_signals import center, distance


BALL_ASSOCIATION_MARGIN = 0.25


def top_down(
    objects: list, active_ball: dict, court: dict | None, calibration: dict
) -> dict:
    """Include visible player feet; locate a ball only beside an unambiguous player.

    A single image ray does not locate an airborne ball on the floor. Proximity
    is an estimate, not a possession event, and never becomes a training label.
    """
    dimensions = (
        {k: court[k] for k in ("length", "width")}
        if court
        else {"length": 40, "width": 20}
    )
    visible = [o for o in objects if o["label"] == "player"]
    players = [
        {
            "track_id": o["track_id"],
            "display_id": o.get("display_id"),
            "team": o.get("team", "unknown"),
            "xy": o["court_xy_m"],
            "estimated": True,
        }
        for o in visible
        if o.get("court_xy_m") is not None
        and all(
            0 <= v <= dimensions[k]
            for v, k in zip(o["court_xy_m"], ("length", "width"), strict=True)
        )
    ]
    result = {
        "court": dimensions,
        "status": calibration.get("status", "unknown"),
        "players": players,
        "unplaced_players": len(visible) - len(players),
        "ball": None,
        "ball_status": "not_observed",
    }
    ball = next(
        (
            o
            for o in objects
            if o["label"] == "ball" and o.get("track_id") == active_ball.get("track_id")
        ),
        None,
    )
    if ball is None:
        return result
    result["ball_status"] = "position_unknown"
    point = center(ball["bbox"])
    candidates = []
    available = {p["track_id"]: p for p in players}
    for obj in visible:
        if obj["track_id"] not in available:
            continue
        x, y, w, h = obj.get("observed_bbox", obj["bbox"])
        if (
            x - w * 0.15 <= point[0] <= x + w * 1.15
            and y + h * 0.15 <= point[1] <= y + h * 0.8
        ):
            score = distance([(point[0] - x) / w, (point[1] - y) / h], [0.5, 0.45])
            candidates.append((score, obj["track_id"]))
    candidates.sort()
    if not candidates or (
        len(candidates) > 1
        and candidates[1][0] - candidates[0][0] < BALL_ASSOCIATION_MARGIN
    ):
        return result
    player = available[candidates[0][1]]
    result.update(
        ball_status="near_player_estimate",
        ball={
            "xy": player["xy"],
            "track_id": ball["track_id"],
            "near_player": player["track_id"],
            "estimated": True,
        },
    )
    return result
