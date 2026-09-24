"""Top-down observations with explicit uncertainty about elevated balls."""

from operator import itemgetter

from .clip_signals import center, distance


BALL_ASSOCIATION_MARGIN = 0.25


def top_down(
    objects: list, active_ball: dict, court: dict | None, calibration: dict
) -> dict:
    """Include measured feet and marked predictions, with measured-only ball proximity.

    A single image ray does not locate an airborne ball on the floor. Proximity
    is an estimate, not a possession event, and never becomes a training label.
    """
    dimensions = (
        {k: court[k] for k in ("length", "width")}
        if court
        else {"length": 40, "width": 20}
    )
    visible = [o for o in objects if o["label"] == "player"]
    players = []
    for obj in visible:
        prediction = obj.get("court_prediction") or {}
        xy = obj.get("court_xy_m")
        predicted = xy is None
        if predicted:
            xy = prediction.get("xy")
        if xy is None or not all(
            0 <= v <= dimensions[k]
            for v, k in zip(xy, ("length", "width"), strict=True)
        ):
            continue
        players.append({
            "track_id": obj["track_id"],
            "display_id": obj.get("display_id"),
            "team": obj.get("team", "unknown"),
            "xy": xy,
            "estimated": True,
            "court_half": obj.get("court_half", "unknown"),
            "half_group": obj.get("half_group", "unknown"),
            **(prediction if predicted else {}),
        })
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
    player = near_player(
        ball, [o for o in visible if o["track_id"] in {p["track_id"] for p in players}]
    )
    if player is None:
        return result
    result.update(
        ball_status="near_player_estimate",
        ball={
            "xy": player["court_xy_m"],
            "track_id": ball["track_id"],
            "near_player": player["track_id"],
            "estimated": True,
        },
    )
    return result


def near_player(ball: dict, people: list[dict]) -> dict | None:
    """Return a uniquely nearby mapped torso, never infer possession from overlap."""
    point = center(ball["bbox"])
    candidates = []
    for obj in people:
        if obj.get("label") != "player" or obj.get("court_xy_m") is None:
            continue
        x, y, w, h = obj.get("observed_bbox", obj["bbox"])
        if (
            w > 0
            and h > 0
            and x - w * 0.15 <= point[0] <= x + w * 1.15
            and y + h * 0.15 <= point[1] <= y + h * 0.8
        ):
            score = distance([(point[0] - x) / w, (point[1] - y) / h], [0.5, 0.45])
            candidates.append((score, obj))
    candidates.sort(key=itemgetter(0))
    if any(obj.get("identity_uncertain") for _, obj in candidates):
        return None
    if not candidates or (
        len(candidates) > 1
        and candidates[1][0] - candidates[0][0] < BALL_ASSOCIATION_MARGIN
    ):
        return None
    return candidates[0][1]
