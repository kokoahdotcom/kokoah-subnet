"""The incentive mechanism as pure functions (specification v5.6). Nothing here touches a network."""

from .credit import DayFees, day_fees, ema
from .exclusions import (
    MirrorAction,
    clip_to_mark,
    drop_cluster_matches,
    mirror_action,
    mirror_position_share,
    mirror_timing_notional,
)
from .fills import Fill, by_user, match_total_fees, parse_builder_csv, parse_user_fills
from .gates import active_days, passes_gates, round_trips
from .holding import holding_factor
from .params import DEFAULT, SPEC_VERSION, Params
from .payout import Allocation, MinerDay, allocate, cap_usd, multiplier, to_u16, weights
from .skill import SkillParts, daily_return, skill_factor, skill_raw
from .state import MinerState, advance

__all__ = [
    "DEFAULT",
    "SPEC_VERSION",
    "Allocation",
    "DayFees",
    "Fill",
    "MinerDay",
    "MinerState",
    "MirrorAction",
    "Params",
    "SkillParts",
    "active_days",
    "advance",
    "allocate",
    "by_user",
    "cap_usd",
    "clip_to_mark",
    "daily_return",
    "day_fees",
    "drop_cluster_matches",
    "ema",
    "holding_factor",
    "match_total_fees",
    "mirror_action",
    "mirror_position_share",
    "mirror_timing_notional",
    "multiplier",
    "parse_builder_csv",
    "parse_user_fills",
    "passes_gates",
    "round_trips",
    "skill_factor",
    "skill_raw",
    "to_u16",
    "weights",
]
