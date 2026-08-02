"""Regression tests for GitHub issue #15: a physical train that terminates
at a station, reverses direction, and continues under a *different*
trip_id (e.g. Tadworth -> Purley -> London Bridge) is invisible to
find_direct_trips unless db._build_trip_continuations synthesizes a
combined trip covering both legs. A synthetic feed isolates this — the
real checked-in fixture doesn't model any reversal.

Covers, per PLAN.md's Opus-reviewed test list:
  - the two real cases from the issue (Tadworth/Purley, Earlswood/Redhill —
    the latter's 4-minute connection is below MIN_CONNECTION_TIME_MINUTES,
    which is exactly why find_interchange_trips alone can't find it)
  - no duplicate direct result for a query that lies entirely within one leg
  - a decoy pair just outside the reversal-dwell cap is NOT synthesized
  - an ambiguous match, in both directions, is skipped rather than guessed
  - a blank trip_short_name never matches
  - a reversal spanning physical midnight (>=24:00:00 GTFS notation)

Plus, per a real correctness bug found live against the production feed
(2026-08-02) after the original origin-only join was measured to find zero
of the real feed's actual reversals:
  - a portion join, where leg 2 doesn't originate at the reversal stop but
    calls there mid-trip having already started a stop earlier (the actual
    shape of both real issue #15 cases)
  - a same-headcode/same-service group large enough to be headcode noise
    (mirrors the real feed's "0B00" placeholder) is never synthesized, even
    when an otherwise-valid reversal pair sits inside it
  - a group exactly at the size cap is still accepted (boundary case)
  - a continuing trip that calls at the reversal stop more than once inside
    the dwell window (e.g. an out-and-back working) is skipped as
    ambiguous rather than an arbitrary occurrence being picked
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd
import pytest

from app import config, queries
from app.db import build_database, get_connection

STOPS = pd.DataFrame(
    [
        {"stop_id": "S_TAD", "stop_code": "TAD", "stop_name": "Tadworth"},
        {"stop_id": "S_PUR", "stop_code": "PUR", "stop_name": "Purley"},
        {"stop_id": "S_LBG", "stop_code": "LBG", "stop_name": "London Bridge"},
        {"stop_id": "S_ERL", "stop_code": "ERL", "stop_name": "Earlswood"},
        {"stop_id": "S_RED", "stop_code": "RED", "stop_name": "Redhill"},
        {"stop_id": "S_VIC", "stop_code": "VIC", "stop_name": "Victoria"},
        {"stop_id": "S_FOJ", "stop_code": "FOJ", "stop_name": "Foo Junction"},
        {"stop_id": "S_BAR", "stop_code": "BAR", "stop_name": "Bar Parkway"},
        {"stop_id": "S_BAZ", "stop_code": "BAZ", "stop_name": "Baz Central"},
        {"stop_id": "S_AMA", "stop_code": "AMA", "stop_name": "Ambiguous A"},
        {"stop_id": "S_AMR", "stop_code": "AMR", "stop_name": "Ambiguous Reversal"},
        {"stop_id": "S_AB1", "stop_code": "AB1", "stop_name": "Ambiguous B1"},
        {"stop_id": "S_AB2", "stop_code": "AB2", "stop_name": "Ambiguous B2"},
        {"stop_id": "S_AN1", "stop_code": "AN1", "stop_name": "Ambiguous N1"},
        {"stop_id": "S_AN2", "stop_code": "AN2", "stop_name": "Ambiguous N2"},
        {"stop_id": "S_ANR", "stop_code": "ANR", "stop_name": "Ambiguous N Reversal"},
        {"stop_id": "S_AND", "stop_code": "AND", "stop_name": "Ambiguous N Dest"},
        {"stop_id": "S_BLA", "stop_code": "BLA", "stop_name": "Blank A"},
        {"stop_id": "S_BLR", "stop_code": "BLR", "stop_name": "Blank Reversal"},
        {"stop_id": "S_BLD", "stop_code": "BLD", "stop_name": "Blank D"},
        {"stop_id": "S_MNA", "stop_code": "MNA", "stop_name": "Midnight A"},
        {"stop_id": "S_MNR", "stop_code": "MNR", "stop_name": "Midnight Reversal"},
        {"stop_id": "S_MND", "stop_code": "MND", "stop_name": "Midnight D"},
        {"stop_id": "S_RA", "stop_code": "RRA", "stop_name": "Retrace A"},
        {"stop_id": "S_RB", "stop_code": "RRB", "stop_name": "Retrace B"},
        {"stop_id": "S_RREV", "stop_code": "RRV", "stop_name": "Retrace Reversal"},
        {"stop_id": "S_RZ", "stop_code": "RRZ", "stop_name": "Retrace Z"},
        {"stop_id": "S_CA", "stop_code": "CCA", "stop_name": "Chain A"},
        {"stop_id": "S_CB", "stop_code": "CCB", "stop_name": "Chain B"},
        {"stop_id": "S_CC", "stop_code": "CCC", "stop_name": "Chain C"},
        {"stop_id": "S_CD", "stop_code": "CCD", "stop_name": "Chain D"},
        {"stop_id": "S_PJA", "stop_code": "PJA", "stop_name": "Portion Join A"},
        {"stop_id": "S_PJR", "stop_code": "PJR", "stop_name": "Portion Join Reversal"},
        {"stop_id": "S_PJP", "stop_code": "PJP", "stop_name": "Portion Join Pre"},
        {"stop_id": "S_PJQ", "stop_code": "PJQ", "stop_name": "Portion Join Post"},
        {"stop_id": "S_LGA", "stop_code": "LGA", "stop_name": "Large Group A"},
        {"stop_id": "S_LGR", "stop_code": "LGR", "stop_name": "Large Group Reversal"},
        {"stop_id": "S_LGB", "stop_code": "LGB", "stop_name": "Large Group B"},
        {"stop_id": "S_LGC", "stop_code": "LGC", "stop_name": "Large Group C"},
        {"stop_id": "S_LGD", "stop_code": "LGD", "stop_name": "Large Group D"},
        {"stop_id": "S_LGE", "stop_code": "LGE", "stop_name": "Large Group E"},
        {"stop_id": "S_LGF", "stop_code": "LGF", "stop_name": "Large Group F"},
        {"stop_id": "S_CP1", "stop_code": "CP1", "stop_name": "Cap Boundary 1"},
        {"stop_id": "S_CPR", "stop_code": "CPR", "stop_name": "Cap Boundary Reversal"},
        {"stop_id": "S_CP2", "stop_code": "CP2", "stop_name": "Cap Boundary 2"},
        {"stop_id": "S_CPN1", "stop_code": "CN1", "stop_name": "Cap Boundary Noise 1"},
        {"stop_id": "S_CPN2", "stop_code": "CN2", "stop_name": "Cap Boundary Noise 2"},
        {"stop_id": "S_DCX", "stop_code": "DCX", "stop_name": "Double Call X"},
        {"stop_id": "S_DCR", "stop_code": "DCR", "stop_name": "Double Call Reversal"},
        {"stop_id": "S_DCY", "stop_code": "DCY", "stop_name": "Double Call Y"},
        {"stop_id": "S_DCZ", "stop_code": "DCZ", "stop_name": "Double Call Z"},
        {"stop_id": "S_DC1", "stop_code": "DC1", "stop_name": "Double Call Origin"},
    ]
)
ROUTES = pd.DataFrame(
    [{"route_id": f"R{i}", "agency_id": "TT", "route_short_name": "", "route_long_name": f"Route {i}"} for i in range(1, 35)]
)
AGENCY = pd.DataFrame([{"agency_id": "TT", "agency_name": "Test Trains"}])

MONDAY = dt.date(2026, 8, 3)


def _stop_time(trip_id, stop_id, seq, hms):
    return {
        "trip_id": trip_id,
        "stop_id": stop_id,
        "stop_sequence": seq,
        "arrival_time": hms,
        "departure_time": hms,
    }


def _calendar_row(service_id, day, start, end):
    row = {d: 0 for d in queries.DAY_COLUMNS}
    row[day] = 1
    return {"service_id": service_id, "start_date": start, "end_date": end, **row}


@pytest.fixture(scope="module")
def db_path(tmp_path_factory) -> Path:
    gap_min = config.REVERSAL_MAX_DWELL_MINUTES

    def _dep(base_hh, base_mm, extra_min):
        total = base_hh * 60 + base_mm + extra_min
        return f"{total // 60:02d}:{total % 60:02d}:00"

    trips = [
        # Scenario 1: Tadworth -> Purley -> London Bridge (issue #15's
        # first confirmed case).
        {"route_id": "R1", "service_id": "SVC1", "trip_id": "T_TAD_PUR", "trip_headsign": "Purley", "trip_short_name": "1937"},
        {"route_id": "R2", "service_id": "SVC1", "trip_id": "T_PUR_LBG", "trip_headsign": "London Bridge", "trip_short_name": "1937"},
        # Scenario 2: Earlswood -> Redhill -> Victoria (issue #15's second
        # confirmed case — 4-minute connection, below MIN_CONNECTION_TIME_MINUTES).
        {"route_id": "R3", "service_id": "SVC1", "trip_id": "T_ERL_RED", "trip_headsign": "Redhill", "trip_short_name": "1005"},
        {"route_id": "R4", "service_id": "SVC1", "trip_id": "T_RED_VIC", "trip_headsign": "London Victoria", "trip_short_name": "1005"},
        # Scenario 3: decoy pair just outside the reversal-dwell cap —
        # must NOT synthesize.
        {"route_id": "R5", "service_id": "SVC1", "trip_id": "T_FOJ_BAR", "trip_headsign": "Bar Parkway", "trip_short_name": "2000"},
        {"route_id": "R6", "service_id": "SVC1", "trip_id": "T_BAR_BAZ", "trip_headsign": "Baz Central", "trip_short_name": "2000"},
        # Scenario 4: ambiguous on the *terminating* side — T_AMA_AMR
        # matches two same-headcode departures from AMR.
        {"route_id": "R7", "service_id": "SVC1", "trip_id": "T_AMA_AMR", "trip_headsign": "Ambiguous Reversal", "trip_short_name": "3001"},
        {"route_id": "R8", "service_id": "SVC1", "trip_id": "T_AMR_AB1", "trip_headsign": "Ambiguous B1", "trip_short_name": "3001"},
        {"route_id": "R9", "service_id": "SVC1", "trip_id": "T_AMR_AB2", "trip_headsign": "Ambiguous B2", "trip_short_name": "3001"},
        # Scenario 5: ambiguous on the *departing* side — T_ANR_AND is
        # matched by two same-headcode terminating trips.
        {"route_id": "R10", "service_id": "SVC1", "trip_id": "T_AN1_ANR", "trip_headsign": "Ambiguous N Reversal", "trip_short_name": "3002"},
        {"route_id": "R11", "service_id": "SVC1", "trip_id": "T_AN2_ANR", "trip_headsign": "Ambiguous N Reversal", "trip_short_name": "3002"},
        {"route_id": "R12", "service_id": "SVC1", "trip_id": "T_ANR_AND", "trip_headsign": "Ambiguous N Dest", "trip_short_name": "3002"},
        # Scenario 6: blank trip_short_name — must never match, even with
        # a short, plausible-looking dwell.
        {"route_id": "R13", "service_id": "SVC1", "trip_id": "T_BLA_BLR", "trip_headsign": "Blank Reversal", "trip_short_name": ""},
        {"route_id": "R14", "service_id": "SVC1", "trip_id": "T_BLR_BLD", "trip_headsign": "Blank D", "trip_short_name": ""},
        # Scenario 7: reversal spanning physical midnight (>=24:00:00 GTFS
        # notation, same service day).
        {"route_id": "R15", "service_id": "SVC1", "trip_id": "T_MNA_MNR", "trip_headsign": "Midnight Reversal", "trip_short_name": "4001"},
        {"route_id": "R16", "service_id": "SVC1", "trip_id": "T_MNR_MND", "trip_headsign": "Midnight D", "trip_short_name": "4001"},
        # Scenario 8: retracing reversal — leg 2 heads back out through a
        # stop leg 1 already called at (RB), so the synthesized trip
        # contains RB twice.
        {"route_id": "R17", "service_id": "SVC1", "trip_id": "T_RA_RREV", "trip_headsign": "Retrace Reversal", "trip_short_name": "5001"},
        {"route_id": "R18", "service_id": "SVC1", "trip_id": "T_RREV_RZ", "trip_headsign": "Retrace Z", "trip_short_name": "5001"},
        # Scenario 9: a train that reverses twice (C1 -> C2 -> C3, same
        # headcode throughout) — pairwise synthesis gives C1+C2 and C2+C3
        # but never a single trip covering all three legs.
        {"route_id": "R19", "service_id": "SVC1", "trip_id": "T_C1", "trip_headsign": "Chain B", "trip_short_name": "6001"},
        {"route_id": "R20", "service_id": "SVC1", "trip_id": "T_C2", "trip_headsign": "Chain C", "trip_short_name": "6001"},
        {"route_id": "R21", "service_id": "SVC1", "trip_id": "T_C3", "trip_headsign": "Chain D", "trip_short_name": "6001"},
        # Scenario 10: portion join — leg 2 does NOT originate at the
        # reversal stop, it originates one stop earlier and just *calls* at
        # it mid-trip before continuing — the actual shape of both of issue
        # #15's real confirmed cases (found live against the production
        # feed, 2026-08-02). The synthesized trip must start from the join
        # stop onward and drop leg 2's earlier, pre-join stop entirely.
        {"route_id": "R22", "service_id": "SVC1", "trip_id": "T_PJA_PJR", "trip_headsign": "Portion Join Reversal", "trip_short_name": "7001"},
        {"route_id": "R23", "service_id": "SVC1", "trip_id": "T_PJP_PJQ", "trip_headsign": "Portion Join Post", "trip_short_name": "7001"},
        # Scenario 11: large headcode group (like the real feed's "0B00"
        # placeholder) — an otherwise-valid reversal pair (LG1/LG2) must NOT
        # synthesize because the (trip_short_name, service_id) group has 4
        # members, above REVERSAL_MAX_HEADCODE_GROUP_SIZE (3). LG3/LG4 are
        # unrelated noise trips that exist purely to inflate the group size.
        {"route_id": "R24", "service_id": "SVC1", "trip_id": "T_LG1", "trip_headsign": "Large Group Reversal", "trip_short_name": "0B00"},
        {"route_id": "R25", "service_id": "SVC1", "trip_id": "T_LG2", "trip_headsign": "Large Group B", "trip_short_name": "0B00"},
        {"route_id": "R26", "service_id": "SVC1", "trip_id": "T_LG3", "trip_headsign": "Large Group D", "trip_short_name": "0B00"},
        {"route_id": "R27", "service_id": "SVC1", "trip_id": "T_LG4", "trip_headsign": "Large Group F", "trip_short_name": "0B00"},
        # Scenario 12: group exactly at REVERSAL_MAX_HEADCODE_GROUP_SIZE (3) —
        # boundary case, must still be accepted (not off-by-one rejected).
        # CPN1/CPN2 is unrelated noise sharing the same headcode/service,
        # purely to bring the group to exactly 3.
        {"route_id": "R28", "service_id": "SVC1", "trip_id": "T_CP1_CPR", "trip_headsign": "Cap Boundary Reversal", "trip_short_name": "8001"},
        {"route_id": "R29", "service_id": "SVC1", "trip_id": "T_CPR_CP2", "trip_headsign": "Cap Boundary 2", "trip_short_name": "8001"},
        {"route_id": "R30", "service_id": "SVC1", "trip_id": "T_CPN1_CPN2", "trip_headsign": "Cap Boundary Noise 2", "trip_short_name": "8001"},
        # Scenario 13: leg 2 calls at the reversal stop twice inside the
        # dwell window (an out-and-back working) — must be skipped as
        # ambiguous, not resolved by picking either occurrence.
        {"route_id": "R31", "service_id": "SVC1", "trip_id": "T_DC1_DCR", "trip_headsign": "Double Call Reversal", "trip_short_name": "9001"},
        {"route_id": "R32", "service_id": "SVC1", "trip_id": "T_DCX_DCY", "trip_headsign": "Double Call Y", "trip_short_name": "9001"},
    ]

    over_cap = _dep(10, 20, gap_min + 1)  # BAR departure, strictly outside the cap

    stop_times = [
        # Scenario 1 — 21:19 -> 21:32 (terminus) -> 21:36 -> 22:12 (4-minute reversal dwell).
        _stop_time("T_TAD_PUR", "S_TAD", 1, "21:19:00"),
        _stop_time("T_TAD_PUR", "S_PUR", 2, "21:32:00"),
        _stop_time("T_PUR_LBG", "S_PUR", 1, "21:36:00"),
        _stop_time("T_PUR_LBG", "S_LBG", 2, "22:12:00"),
        # Scenario 2 — 08:32:30 -> 08:36 (terminus) -> 08:40 -> 09:19 (4-minute dwell).
        _stop_time("T_ERL_RED", "S_ERL", 1, "08:32:30"),
        _stop_time("T_ERL_RED", "S_RED", 2, "08:36:00"),
        _stop_time("T_RED_VIC", "S_RED", 1, "08:40:00"),
        _stop_time("T_RED_VIC", "S_VIC", 2, "09:19:00"),
        # Scenario 3 — decoy, dwell = cap + 1 minute.
        _stop_time("T_FOJ_BAR", "S_FOJ", 1, "10:00:00"),
        _stop_time("T_FOJ_BAR", "S_BAR", 2, "10:20:00"),
        _stop_time("T_BAR_BAZ", "S_BAR", 1, over_cap),
        _stop_time("T_BAR_BAZ", "S_BAZ", 2, "10:50:00"),
        # Scenario 4 — one terminus, two same-headcode departures.
        _stop_time("T_AMA_AMR", "S_AMA", 1, "11:00:00"),
        _stop_time("T_AMA_AMR", "S_AMR", 2, "11:10:00"),
        _stop_time("T_AMR_AB1", "S_AMR", 1, "11:12:00"),
        _stop_time("T_AMR_AB1", "S_AB1", 2, "11:30:00"),
        _stop_time("T_AMR_AB2", "S_AMR", 1, "11:14:00"),
        _stop_time("T_AMR_AB2", "S_AB2", 2, "11:35:00"),
        # Scenario 5 — two termini, one same-headcode departure.
        _stop_time("T_AN1_ANR", "S_AN1", 1, "12:00:00"),
        _stop_time("T_AN1_ANR", "S_ANR", 2, "12:10:00"),
        _stop_time("T_AN2_ANR", "S_AN2", 1, "12:01:00"),
        _stop_time("T_AN2_ANR", "S_ANR", 2, "12:12:00"),
        _stop_time("T_ANR_AND", "S_ANR", 1, "12:16:00"),
        _stop_time("T_ANR_AND", "S_AND", 2, "12:30:00"),
        # Scenario 6 — blank headcode, short dwell.
        _stop_time("T_BLA_BLR", "S_BLA", 1, "13:00:00"),
        _stop_time("T_BLA_BLR", "S_BLR", 2, "13:10:00"),
        _stop_time("T_BLR_BLD", "S_BLR", 1, "13:12:00"),
        _stop_time("T_BLR_BLD", "S_BLD", 2, "13:30:00"),
        # Scenario 7 — post-midnight, >=24:00:00 notation, 4-minute dwell.
        _stop_time("T_MNA_MNR", "S_MNA", 1, "23:50:00"),
        _stop_time("T_MNA_MNR", "S_MNR", 2, "24:02:00"),
        _stop_time("T_MNR_MND", "S_MNR", 1, "24:06:00"),
        _stop_time("T_MNR_MND", "S_MND", 2, "24:20:00"),
        # Scenario 8 — retracing reversal: RA -> RB -> RREV (terminus),
        # reverses, RREV -> RB (again) -> RZ. 4-minute dwell.
        _stop_time("T_RA_RREV", "S_RA", 1, "09:00:00"),
        _stop_time("T_RA_RREV", "S_RB", 2, "09:10:00"),
        _stop_time("T_RA_RREV", "S_RREV", 3, "09:20:00"),
        _stop_time("T_RREV_RZ", "S_RREV", 1, "09:24:00"),
        _stop_time("T_RREV_RZ", "S_RB", 2, "09:40:00"),
        _stop_time("T_RREV_RZ", "S_RZ", 3, "09:50:00"),
        # Scenario 9 — double reversal: CA->CB (terminus) -> CB->CC
        # (terminus) -> CC->CD, all headcode 6001. 8-minute dwell each —
        # deliberately *above* MIN_CONNECTION_TIME_MINUTES (5) and still
        # within REVERSAL_MAX_DWELL_MINUTES (10), so the old MCT floor
        # can't be the thing blocking find_interchange_trips from offering
        # a bogus interchange here — this test needs the constituent-trip
        # guard itself to be doing the work, not the MCT as a side effect
        # (a 4-minute dwell would make this assertion pass for the wrong
        # reason — found in code review, 2026-08-01).
        _stop_time("T_C1", "S_CA", 1, "14:00:00"),
        _stop_time("T_C1", "S_CB", 2, "14:10:00"),
        _stop_time("T_C2", "S_CB", 1, "14:18:00"),
        _stop_time("T_C2", "S_CC", 2, "14:28:00"),
        _stop_time("T_C3", "S_CC", 1, "14:36:00"),
        _stop_time("T_C3", "S_CD", 2, "14:48:00"),
        # Scenario 10 — portion join: leg 2 originates at PJP (before the
        # reversal stop), calls at PJR (the reversal stop) at sequence 2,
        # then continues to PJQ. 4-minute dwell (15:20 arrival -> 15:24
        # departure), same as issue #15's real cases.
        _stop_time("T_PJA_PJR", "S_PJA", 1, "15:00:00"),
        _stop_time("T_PJA_PJR", "S_PJR", 2, "15:20:00"),
        _stop_time("T_PJP_PJQ", "S_PJP", 1, "15:05:00"),
        _stop_time("T_PJP_PJQ", "S_PJR", 2, "15:24:00"),
        _stop_time("T_PJP_PJQ", "S_PJQ", 3, "15:50:00"),
        # Scenario 11 — large headcode group: LG1 -> LG2 would otherwise be
        # a valid 4-minute-dwell reversal, but the group (0B00, SVC1) has 4
        # members (LG1-LG4), above the cap — must not synthesize.
        _stop_time("T_LG1", "S_LGA", 1, "16:00:00"),
        _stop_time("T_LG1", "S_LGR", 2, "16:10:00"),
        _stop_time("T_LG2", "S_LGR", 1, "16:14:00"),
        _stop_time("T_LG2", "S_LGB", 2, "16:30:00"),
        _stop_time("T_LG3", "S_LGC", 1, "17:00:00"),
        _stop_time("T_LG3", "S_LGD", 2, "17:20:00"),
        _stop_time("T_LG4", "S_LGE", 1, "18:00:00"),
        _stop_time("T_LG4", "S_LGF", 2, "18:20:00"),
        # Scenario 12 — group of exactly 3 (T_CP1_CPR/T_CPR_CP2's genuine
        # pair, plus T_CPN1_CPN2 as unrelated noise sharing the headcode)
        # must still be accepted. 4-minute dwell.
        _stop_time("T_CP1_CPR", "S_CP1", 1, "19:00:00"),
        _stop_time("T_CP1_CPR", "S_CPR", 2, "19:10:00"),
        _stop_time("T_CPR_CP2", "S_CPR", 1, "19:14:00"),
        _stop_time("T_CPR_CP2", "S_CP2", 2, "19:30:00"),
        _stop_time("T_CPN1_CPN2", "S_CPN1", 1, "20:00:00"),
        _stop_time("T_CPN1_CPN2", "S_CPN2", 2, "20:20:00"),
        # Scenario 13 — leg 2 (T_DCX_DCY) calls at the reversal stop (DCR)
        # twice within the dwell window: once outbound (19:02, seq 2) and
        # again on the way back (19:05, seq 4), both within 10 minutes of
        # leg 1's 19:00 terminus arrival. A final stop (DCZ) after the
        # second DCR call ensures neither occurrence is leg 2's own
        # terminus (which is never a valid join point). Neither occurrence
        # should be used.
        _stop_time("T_DC1_DCR", "S_DC1", 1, "18:50:00"),
        _stop_time("T_DC1_DCR", "S_DCR", 2, "19:00:00"),
        _stop_time("T_DCX_DCY", "S_DCX", 1, "18:50:00"),
        _stop_time("T_DCX_DCY", "S_DCR", 2, "19:02:00"),
        _stop_time("T_DCX_DCY", "S_DCY", 3, "19:04:00"),
        _stop_time("T_DCX_DCY", "S_DCR", 4, "19:05:00"),
        _stop_time("T_DCX_DCY", "S_DCZ", 5, "19:10:00"),
    ]
    calendar = [_calendar_row("SVC1", "monday", "20260803", "20260803")]

    gtfs_dir = tmp_path_factory.mktemp("gtfs_trip_continuations")
    STOPS.to_csv(gtfs_dir / "stops.txt", index=False)
    AGENCY.to_csv(gtfs_dir / "agency.txt", index=False)
    ROUTES.to_csv(gtfs_dir / "routes.txt", index=False)
    pd.DataFrame(trips).to_csv(gtfs_dir / "trips.txt", index=False)
    pd.DataFrame(stop_times).to_csv(gtfs_dir / "stop_times.txt", index=False)
    pd.DataFrame(calendar).to_csv(gtfs_dir / "calendar.txt", index=False)
    pd.DataFrame(columns=["service_id", "date", "exception_type"]).to_csv(
        gtfs_dir / "calendar_dates.txt", index=False
    )

    db_path = gtfs_dir / "gtfs.db"
    build_database(gtfs_dir, db_path)
    return db_path


@pytest.fixture
def conn(db_path):
    connection = get_connection(db_path)
    try:
        yield connection
    finally:
        connection.close()


def test_reversal_synthesizes_a_seamless_direct_journey(conn):
    """Issue #15's headline case: Tadworth -> Purley -> London Bridge is
    invisible without the fix."""
    origin = queries.get_station(conn, "TAD")
    destination = queries.get_station(conn, "LBG")

    results = queries.find_direct_trips(conn, origin, destination, MONDAY, dt.time(21, 0), 60)

    assert len(results) == 1
    trip = results[0]
    assert trip.departure_time == "21:19:00"
    assert trip.arrival_time == "22:12:00"
    assert trip.duration_minutes == 53
    assert trip.reverses_at is not None
    assert trip.reverses_at.stop_code == "PUR"


def test_reversal_below_min_connection_time_still_found_directly(conn):
    """Issue #15's second case: the real connection (4 min) is below
    MIN_CONNECTION_TIME_MINUTES, so find_interchange_trips alone can never
    find it — only the synthesized direct trip does."""
    origin = queries.get_station(conn, "ERL")
    destination = queries.get_station(conn, "VIC")

    assert 4 < config.MIN_CONNECTION_TIME_MINUTES, "test assumes the real gap stays below the passenger MCT"

    direct = queries.find_direct_trips(conn, origin, destination, MONDAY, dt.time(8, 0), 60)
    assert len(direct) == 1
    assert direct[0].departure_time == "08:32:30"
    assert direct[0].arrival_time == "09:19:00"

    interchange = queries.find_interchange_trips(conn, origin, destination, MONDAY, dt.time(8, 0), 60)
    assert interchange == [], "the 4-minute connection is below MCT, so the old interchange path finds nothing"


def test_intra_leg_query_does_not_duplicate_the_plain_trip(conn):
    """A query lying entirely within one constituent leg must match only
    the plain trip, not also the synthesized combined trip covering it."""
    origin = queries.get_station(conn, "TAD")
    destination = queries.get_station(conn, "PUR")
    results = queries.find_direct_trips(conn, origin, destination, MONDAY, dt.time(21, 0), 60)
    assert [t.trip_id for t in results] == ["T_TAD_PUR"]

    origin = queries.get_station(conn, "PUR")
    destination = queries.get_station(conn, "LBG")
    results = queries.find_direct_trips(conn, origin, destination, MONDAY, dt.time(21, 0), 60)
    assert [t.trip_id for t in results] == ["T_PUR_LBG"]


def test_interchange_does_not_offer_the_same_physical_train_as_a_change(conn):
    """find_journeys should surface the reversal exactly once, as a 0-change
    direct journey — not also as a bogus 1-change interchange at the
    reversal stop for the same physical train."""
    origin = queries.get_station(conn, "TAD")
    destination = queries.get_station(conn, "LBG")
    journeys = queries.find_journeys(conn, origin, destination, MONDAY, dt.time(21, 0), 60)
    assert len(journeys) == 1
    assert journeys[0].kind == "direct"
    assert journeys[0].direct.trip_id == "T_TAD_PUR+T_PUR_LBG"


def test_decoy_pair_just_outside_dwell_cap_is_not_synthesized(conn):
    origin = queries.get_station(conn, "FOJ")
    destination = queries.get_station(conn, "BAZ")
    assert queries.find_direct_trips(conn, origin, destination, MONDAY, dt.time(9, 0), 120) == []

    # Both legs still individually queryable — the pair just isn't stitched together.
    leg1 = queries.find_direct_trips(conn, origin, queries.get_station(conn, "BAR"), MONDAY, dt.time(9, 0), 120)
    assert [t.trip_id for t in leg1] == ["T_FOJ_BAR"]


def test_ambiguous_terminating_trip_is_skipped_on_both_sides(conn):
    """T_AMA_AMR matches two same-headcode departures from AMR — ambiguous,
    so neither should be synthesized."""
    origin = queries.get_station(conn, "AMA")
    assert queries.find_direct_trips(conn, origin, queries.get_station(conn, "AB1"), MONDAY, dt.time(10, 0), 120) == []
    assert queries.find_direct_trips(conn, origin, queries.get_station(conn, "AB2"), MONDAY, dt.time(10, 0), 120) == []


def test_ambiguous_departing_trip_is_skipped_on_both_sides(conn):
    """T_ANR_AND is matched by two same-headcode terminating trips —
    ambiguous, so neither should be synthesized."""
    destination = queries.get_station(conn, "AND")
    assert queries.find_direct_trips(conn, queries.get_station(conn, "AN1"), destination, MONDAY, dt.time(11, 0), 120) == []
    assert queries.find_direct_trips(conn, queries.get_station(conn, "AN2"), destination, MONDAY, dt.time(11, 0), 120) == []


def test_blank_trip_short_name_never_matches(conn):
    origin = queries.get_station(conn, "BLA")
    destination = queries.get_station(conn, "BLD")
    assert queries.find_direct_trips(conn, origin, destination, MONDAY, dt.time(12, 0), 120) == []


def test_reversal_spanning_physical_midnight(conn):
    origin = queries.get_station(conn, "MNA")
    destination = queries.get_station(conn, "MND")
    results = queries.find_direct_trips(conn, origin, destination, MONDAY, dt.time(23, 30), 60)

    assert len(results) == 1
    trip = results[0]
    assert trip.departure_time == "23:50:00"
    assert trip.departure_next_day is False
    assert trip.arrival_time == "00:20:00"
    assert trip.arrival_next_day is True
    assert trip.reverses_at.stop_code == "MNR"


def test_retracing_reversal_does_not_offer_a_bogus_ride_past_and_back(conn):
    """Leg 2 heads back out through RB, a stop leg 1 already called at —
    without the closest-occurrence guard, RA -> RB would additionally match
    the synthesized trip's *later*, post-reversal visit to RB, offering a
    nonsensical "ride to the terminus and back" alongside the real,
    directly-arriving trip (found in code review, 2026-08-01)."""
    origin = queries.get_station(conn, "RRA")
    destination = queries.get_station(conn, "RRB")
    results = queries.find_direct_trips(conn, origin, destination, MONDAY, dt.time(8, 0), 120)
    assert [t.trip_id for t in results] == ["T_RA_RREV"]
    assert results[0].arrival_time == "09:10:00"


def test_retracing_reversal_still_finds_the_genuine_through_journey(conn):
    """The fix for the bogus-revisit case above must not also break the
    genuine case: RA -> RZ is only reachable by going past the reversal."""
    origin = queries.get_station(conn, "RRA")
    destination = queries.get_station(conn, "RRZ")
    results = queries.find_direct_trips(conn, origin, destination, MONDAY, dt.time(8, 0), 120)
    assert len(results) == 1
    assert results[0].departure_time == "09:00:00"
    assert results[0].arrival_time == "09:50:00"
    assert results[0].reverses_at.stop_code == "RRV"


def test_double_reversal_is_not_offered_as_a_fake_interchange(conn):
    """C1 -> C2 -> C3 is one physical train reversing twice (same
    headcode throughout). Pairwise synthesis only ever produces C1+C2 and
    C2+C3, never a single trip spanning all three legs, so CA -> CD is a
    known, scoped-out miss (no direct result) — but find_interchange_trips
    must not offer it as a fake "change trains" journey using two pieces of
    the same physical train (found in code review, 2026-08-01)."""
    assert config.MIN_CONNECTION_TIME_MINUTES < 8 <= config.REVERSAL_MAX_DWELL_MINUTES, (
        "test assumes the fixture's 8-minute dwells clear MCT — otherwise this test would "
        "pass even with a shallow same-trip_id-only guard, since MCT alone would already "
        "block the interchange search from ever reaching it"
    )

    origin = queries.get_station(conn, "CCA")
    destination = queries.get_station(conn, "CCD")

    assert queries.find_direct_trips(conn, origin, destination, MONDAY, dt.time(13, 0), 120) == []
    interchange = queries.find_interchange_trips(conn, origin, destination, MONDAY, dt.time(13, 0), 120)
    assert interchange == [], (
        f"same physical train (C1-C2-C3) offered as a fake interchange: {interchange}"
    )


def test_portion_join_matches_a_mid_trip_call_not_just_an_origin(conn):
    """Both of issue #15's real confirmed cases are portion joins: leg 2
    doesn't originate at the reversal stop, it calls there mid-trip having
    already started a stop earlier — found live against the production
    feed, 2026-08-02, after an origin-only match found zero real reversals.
    The synthesized trip must start from the join stop and drop leg 2's
    earlier, pre-join stop (PJP) entirely."""
    origin = queries.get_station(conn, "PJA")
    destination = queries.get_station(conn, "PJQ")

    results = queries.find_direct_trips(conn, origin, destination, MONDAY, dt.time(14, 0), 120)
    assert len(results) == 1
    trip = results[0]
    assert trip.departure_time == "15:00:00"
    assert trip.arrival_time == "15:50:00"
    assert trip.reverses_at is not None
    assert trip.reverses_at.stop_code == "PJR"

    # Leg 2's own pre-join stop must not appear anywhere in the merge —
    # querying from it should only ever find the plain, unmerged leg 2 trip.
    from_pre = queries.find_direct_trips(
        conn, queries.get_station(conn, "PJP"), destination, MONDAY, dt.time(14, 0), 120
    )
    assert [t.trip_id for t in from_pre] == ["T_PJP_PJQ"]


def test_large_headcode_group_is_never_synthesized(conn):
    """(0B00, SVC1) has 4 members here — mirrors the real feed's "0B00"
    placeholder headcode, carried by ~17% of all trips across every
    operator, which forms enormous same-headcode/same-service groups with
    no genuine reversal relationship. LG1 -> LG2 would otherwise be a valid
    4-minute-dwell reversal, but must be rejected by the group-size cap
    before the dwell/ambiguity checks ever see it."""
    origin = queries.get_station(conn, "LGA")
    destination = queries.get_station(conn, "LGB")
    assert queries.find_direct_trips(conn, origin, destination, MONDAY, dt.time(15, 0), 120) == []

    # Both legs still individually queryable — the pair just isn't stitched together.
    leg1 = queries.find_direct_trips(
        conn, origin, queries.get_station(conn, "LGR"), MONDAY, dt.time(15, 0), 120
    )
    assert [t.trip_id for t in leg1] == ["T_LG1"]


def test_group_exactly_at_the_size_cap_is_still_accepted(conn):
    """A group of exactly REVERSAL_MAX_HEADCODE_GROUP_SIZE (3, headcode
    8001: T_CP1_CPR + T_CPR_CP2's genuine pair, plus T_CPN1_CPN2 as
    unrelated noise) must not be rejected by an off-by-one error in the
    `BETWEEN 2 AND :max_group` bound."""
    assert config.REVERSAL_MAX_HEADCODE_GROUP_SIZE == 3, (
        "test assumes the cap is 3 — this scenario's group has exactly 3 members"
    )
    origin = queries.get_station(conn, "CP1")
    destination = queries.get_station(conn, "CP2")
    results = queries.find_direct_trips(conn, origin, destination, MONDAY, dt.time(18, 0), 120)
    assert len(results) == 1
    assert results[0].trip_id == "T_CP1_CPR+T_CPR_CP2"
    assert results[0].reverses_at.stop_code == "CPR"


def test_continuation_calling_at_reversal_stop_twice_is_skipped_as_ambiguous(conn):
    """T_DCX_DCY calls at the reversal stop (DCR) twice within the dwell
    window (an out-and-back working) — there is no principled way to pick
    which occurrence is the "real" join, so this must be skipped entirely,
    not resolved by arbitrarily using either occurrence. Asserted directly
    against `synthesized_trips`, not indirectly via a specific destination
    station's reachability — an earlier version of this test picked a
    destination (DCY) that an arbitrary-first-or-last-occurrence bug
    happened to still drop, so it passed even when a bogus pair (dropping
    only the *other* occurrence's stops) was silently synthesized. Found
    while deliberately reintroducing that exact bug to verify this test,
    2026-08-02."""
    row = conn.execute(
        "SELECT 1 FROM synthesized_trips WHERE leg1_trip_id = 'T_DC1_DCR' AND leg2_trip_id = 'T_DCX_DCY'"
    ).fetchone()
    assert row is None

    origin = queries.get_station(conn, "DC1")
    destination = queries.get_station(conn, "DCY")
    assert queries.find_direct_trips(conn, origin, destination, MONDAY, dt.time(18, 0), 120) == []

    # Both legs still individually queryable — the pair just isn't stitched together.
    leg1 = queries.find_direct_trips(
        conn, origin, queries.get_station(conn, "DCR"), MONDAY, dt.time(18, 0), 120
    )
    assert [t.trip_id for t in leg1] == ["T_DC1_DCR"]
