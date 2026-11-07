"""End-to-end test: runs the real server over Streamable HTTP and calls every
tool through the official MCP client. Upstream HTTP calls (USGS/TMD) are
replaced with fixtures, so it works offline.

Usage:  python test_client.py            (fixtures, offline)
        python test_client.py --live URL (call a running server, e.g.
                                          http://localhost:8000/mcp, live APIs)
"""

import asyncio
import json
import sys
import threading
import time
from datetime import datetime, timedelta

import uvicorn
from mcp import ClientSession
from mcp.client.streamable_http import streamablehttp_client

import server

USGS_FIXTURE = {
    "type": "FeatureCollection",
    "features": [
        {
            "id": "us7000test",
            "properties": {"mag": 5.8, "place": "10 km N of Chiang Mai, Thailand",
                           "time": 1793000000000, "url": "https://earthquake.usgs.gov/x"},
            "geometry": {"type": "Point", "coordinates": [98.99, 18.85, 10.0]},
        }
    ],
}


SHARED_UTC = "2026-11-06 20:47:48.000"


def tmd_item(utc, thai, mag, depth, lat, lon, title):
    return {"OriginThai": title[:12], "DateTimeUTC": utc, "DateTimeThai": thai, "Depth": depth,
            "Magnitude": mag, "Latitude": lat, "Longitude": lon, "TitleThai": title}


def tmd_fixture(date):
    # Shape of the REAL TMD response: lowercase "header", events under
    # "DailyEarthquakes", numbers as strings, and no event id.
    day = date or "2026-11-07"
    return {
        "@attributes": {"version": "1.0"},
        "header": {"title": "Daily Seismic (Earthquake) Events in Region and global "},
        "DailyEarthquakes": [
            tmd_item(f"{day} 06:54:07.000", f"{day} 13:54:07.000", "4.1", "82", "5.8480000", "94.6050000",
                     "แผ่นดินไหว ตอนเหนือของเกาะสุมาตรา (Northern Sumatra, Indonesia)"),
            tmd_item(SHARED_UTC, "2026-11-07 03:47:48.000", "2.1", "10", "21.2190000", "96.1200000",
                     "แผ่นดินไหว ประเทศเมียนมา (Myanmar)"),
        ],
    }


def check_tmd_mapping():
    ev = server.tmd_item_to_event(tmd_fixture("2026-11-07")["DailyEarthquakes"][0])
    assert ev["id"] == "TMD-20261107065407-5.848_94.605", ev["id"]
    assert ev["time"] == "2026-11-07T06:54:07+00:00", ev["time"]
    assert ev["place"] == "Northern Sumatra, Indonesia", ev["place"]
    assert (ev["magnitude"], ev["depth_km"], ev["latitude"], ev["longitude"]) == (4.1, 82.0, 5.848, 94.605)
    assert ev["source"] == "TMD_DailySeismicEvent" and ev["url"] is None and ev["raw"]["Depth"] == "82"
    # Thai-time only (no UTC field) still gets the +07:00 offset
    thai_only = server.tmd_item_to_event({"DateTimeThai": "2026-11-07 13:54:07.000", "Latitude": "1", "Longitude": "2"})
    assert thai_only["time"] == "2026-11-07T13:54:07+07:00", thai_only["time"]
    # the older shape from the spec still works
    old = server.tmd_item_to_event({"EventID": "660001", "DateTime": "2026-03-07T09:30:15+07:00",
                                    "Magnitude": 4.6, "DepthKm": 10.0, "Latitude": 18.123,
                                    "Longitude": 98.456, "Location": "Near Chiang Mai, Thailand"})
    assert old["id"] == "TMD-660001" and old["time"] == "2026-03-07T09:30:15+07:00"
    assert old["place"] == "Near Chiang Mai, Thailand"
    assert len(server._tmd_items(tmd_fixture(None))) == 2
    assert server._tmd_items({"DailySeismicEvent": [{"EventID": "1"}]}) == [{"EventID": "1"}]
    assert server._tmd_items({"DailyEarthquakes": []}) == []
    print("TMD mapping OK")


async def fake_get_json(url, params):
    if "usgs" in url:
        return USGS_FIXTURE
    if params.get("Date") == "2026-11-05":
        return {"error": "simulated malformed upstream response"}
    return tmd_fixture(params.get("Date"))


def start_local_server(port):
    server._get_json = fake_get_json
    server._today = lambda: datetime(2026, 11, 7, tzinfo=server.LOCAL_TZ)
    cfg = uvicorn.Config(server.mcp.streamable_http_app(), host="127.0.0.1", port=port, log_level="warning")
    srv = uvicorn.Server(cfg)
    threading.Thread(target=srv.run, daemon=True).start()
    for _ in range(50):
        if srv.started:
            return
        time.sleep(0.1)
    raise RuntimeError("server did not start")


def payload(result):
    assert not result.isError, result.content
    if result.structuredContent is not None:
        sc = result.structuredContent
        return sc.get("result", sc) if set(sc) == {"result"} else sc
    return json.loads(result.content[0].text)


async def run(url, live):
    async with streamablehttp_client(url) as (r, w, _):
        async with ClientSession(r, w) as s:
            init = await s.initialize()
            print("serverInfo:", init.serverInfo.name, init.serverInfo.version,
                  "| protocol:", init.protocolVersion)
            tools = (await s.list_tools()).tools
            names = sorted(t.name for t in tools)
            print("tools:", names)
            assert names == sorted([
                "usgs_search_earthquakes", "tmd_get_daily_earthquakes", "tmd_get_recent_earthquakes",
                "match_events_to_assets", "compute_parametric_payout"]), names

            today = server._today().date()
            usgs = payload(await s.call_tool("usgs_search_earthquakes", {
                "starttime": (today - timedelta(days=7)).isoformat(),
                "endtime": today.isoformat(), "limit": 5}))
            print("usgs events:", len(usgs["events"]), "query:", usgs["query"])
            # Use a known event for analytics assertions, independent of live data.
            ev = server.usgs_feature_to_event(USGS_FIXTURE["features"][0]) if live else usgs["events"][0]
            if not live:
                assert ev["id"] == "us7000test" and ev["source"] == "USGS"
                assert ev["latitude"] == 18.85 and ev["longitude"] == 98.99 and ev["depth_km"] == 10.0
                assert ev["time"].endswith("+00:00")

            daily = payload(await s.call_tool("tmd_get_daily_earthquakes", {
                "date": (today - timedelta(days=1)).isoformat()}))
            print("tmd daily:", len(daily["events"]), daily["query"])

            recent = payload(await s.call_tool("tmd_get_recent_earthquakes", {"days": 3}))
            print("tmd recent:", len(recent["events"]), recent["query"], recent.get("warnings"))
            if not live:
                times = [e["time"] for e in recent["events"]]
                assert times == sorted(times, reverse=True)
                assert len(recent["events"]) == 3  # 2 unique days + 1 shared event, de-duplicated
                assert sum(e["raw"]["DateTimeUTC"] == SHARED_UTC for e in recent["events"]) == 1
                assert recent.get("warnings")  # one simulated failing day is reported, not fatal
                invalid_daily = await s.call_tool("tmd_get_daily_earthquakes", {"date": "2026-11-05"})
                assert invalid_daily.isError

            asset_a = {"asset_id": "A-1", "name": "Warehouse A", "latitude": 18.80, "longitude": 98.98,
                       "sum_insured": 1_000_000}
            asset_b = {"asset_id": "B-1", "name": "Plant B", "latitude": 13.75, "longitude": 100.5,
                       "sum_insured": 2_000_000}
            m = payload(await s.call_tool("match_events_to_assets", {
                "events": [ev], "assets": [asset_a, asset_b], "max_distance_km": 100, "min_magnitude": 4.5}))
            print("matches:", [(x["asset_id"], x["events"][0]["distance_km"]) for x in m["matches"]])
            if not live:
                assert [x["asset_id"] for x in m["matches"]] == ["A-1"]

            p = payload(await s.call_tool("compute_parametric_payout", {
                "event": ev, "assets": [asset_a, asset_b]}))
            print("payout:", p["total_payout"], [(a["asset_id"], a["payout_percent"]) for a in p["asset_payouts"]])
            if not live:
                assert p["asset_payouts"][0]["payout_percent"] == 0.5
                assert p["asset_payouts"][0]["payout_amount"] == 500000
                assert p["asset_payouts"][1]["payout_percent"] == 0.0
                assert p["total_payout"] == 500000

            # below trigger -> all zero; custom curve honoured
            low = dict(ev, magnitude=4.9)
            p2 = payload(await s.call_tool("compute_parametric_payout", {"event": low, "assets": [asset_a]}))
            assert p2["total_payout"] == 0
            p3 = payload(await s.call_tool("compute_parametric_payout", {
                "event": ev, "assets": [asset_a],
                "payout_curve": [{"max_distance_km": 3, "payout_percent": 1.0},
                                 {"max_distance_km": 100, "payout_percent": 0.3}]}))
            assert p3["asset_payouts"][0]["payout_percent"] == 0.3, p3

            # validation errors come back as isError results, not crashes
            for name, args in [
                ("usgs_search_earthquakes", {"starttime": "2026-11-01", "endtime": "2026-11-07", "limit": 999}),
                ("usgs_search_earthquakes", {"starttime": "2026-11-01", "endtime": "2026-11-07", "latitude": 1}),
                ("tmd_get_daily_earthquakes", {"date": "07/11/2026"}),
                ("tmd_get_recent_earthquakes", {"days": 30}),
                ("compute_parametric_payout", {"event": {"magnitude": 5}, "assets": [asset_a]}),
                ("compute_parametric_payout", {"event": dict(ev, latitude=91), "assets": [asset_a]}),
                ("compute_parametric_payout", {"event": dict(ev, magnitude="NaN"), "assets": [asset_a]}),
                ("compute_parametric_payout", {"event": ev, "assets": [asset_a], "payout_curve": []}),
                ("compute_parametric_payout", {"event": ev, "assets": [{"asset_id": "x", "latitude": 1, "longitude": 1}]}),
            ]:
                res = await s.call_tool(name, args)
                assert res.isError, (name, args, res.content)
            print("validation errors OK")
    print("ALL TESTS PASSED")


if __name__ == "__main__":
    if len(sys.argv) > 2 and sys.argv[1] == "--live":
        asyncio.run(run(sys.argv[2], live=True))
    else:
        check_tmd_mapping()
        start_local_server(8765)
        asyncio.run(run("http://127.0.0.1:8765/mcp", live=False))
