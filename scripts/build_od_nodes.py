"""Build corridor nodes and supplement IC/JC coordinates."""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections import defaultdict
from pathlib import Path

import _bootstrap  # noqa: F401
import pandas as pd
import shapefile
from pyproj import Transformer

from evdt.io.route import GyeongbuRoute
from evdt.paths import DATA_PROCESSED_DIR


def build_nodes(offices: pd.DataFrame, raw: list[dict], route: GyeongbuRoute):
    """Keep TCS identities separate; flag nearby nodes for explicit reconciliation."""
    nodes = []
    audit = []
    if offices["office_code"].astype(str).duplicated().any():
        raise ValueError("Duplicate TCS office codes")
    for row in offices.to_dict("records"):
        milepost = float(row["milepost_km"])
        if not 0 <= milepost <= route.length_km:
            raise ValueError(f"Office outside corridor: {row['office_name']}")
        for direction in ("UP", "DOWN"):
            offset = route.to_direction(milepost, direction)
            if abs(offset - float(row[f"offset_{direction.lower()}_km"])) > 0.01:
                raise ValueError(f"Office offset mismatch: {row['office_name']}")
            nodes.append(
                dict(
                    node_id=f"tcs:{row['office_code']}",
                    name=row["office_name"],
                    kind="office",
                    direction=direction,
                    milepost_km=milepost,
                    offset_km=offset,
                    office_code=str(row["office_code"]),
                    source=str(row["coordinate_source"]),
                )
            )
    seen = set()
    for row in raw:
        name = str(row["icName"])
        if not name.upper().endswith(("JC", "JCT")):
            continue
        code = str(row["icCode"])
        if code in seen:
            raise ValueError(f"Duplicate JC code: {code}")
        seen.add(code)
        if "estimated_milepost_km" in row:
            milepost = float(row["estimated_milepost_km"])
            distance = 0.0
        else:
            milepost, distance = route.project(float(row["yValue"]), float(row["xValue"]))
        accepted = distance <= 2.0 and 0 < milepost < route.length_km
        source = row.get("position_source", "ic_raw_projection")
        audit.append(
            dict(
                ic_code=code,
                name=name,
                milepost_km=milepost,
                snap_distance_km=None if "estimated_milepost_km" in row else distance,
                position_source=source,
                accepted=accepted,
            )
        )
        if not accepted:
            continue
        for direction in ("UP", "DOWN"):
            nodes.append(
                dict(
                    node_id=f"jc:{code}",
                    name=name,
                    kind="junction",
                    direction=direction,
                    milepost_km=milepost,
                    offset_km=route.to_direction(milepost, direction),
                    office_code=None,
                    source=source,
                )
            )
    for name, milepost in (("Guseo corridor end", 0.0), ("Yangjae corridor end", route.length_km)):
        for direction in ("UP", "DOWN"):
            nodes.append(
                dict(
                    node_id=f"end:{milepost:.3f}",
                    name=name,
                    kind="endpoint",
                    direction=direction,
                    milepost_km=milepost,
                    offset_km=route.to_direction(milepost, direction),
                    office_code=None,
                    source="shared_route",
                )
            )
    result = pd.DataFrame(nodes).sort_values(["direction", "offset_km", "node_id"])
    if result.duplicated(["direction", "node_id"]).any():
        raise ValueError("Duplicate directional node IDs")
    pairs = []
    for direction, group in result.groupby("direction"):
        rows = group.to_dict("records")
        for i, left in enumerate(rows):
            for right in rows[i + 1 :]:
                gap = right["offset_km"] - left["offset_km"]
                if gap > 0.5:
                    break
                pairs.append(
                    dict(
                        direction=direction,
                        left=left["node_id"],
                        right=right["node_id"],
                        gap_km=gap,
                    )
                )
    nearby = pd.DataFrame(pairs, columns=["direction", "left", "right", "gap_km"])
    return (result, pd.DataFrame(audit), nearby)


def build_main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ic-file", type=Path, required=True)
    parser.add_argument("--ic-all-file", type=Path)
    parser.add_argument(
        "--jc-coordinate-file", type=Path, help="Explicitly sourced supplemental JC raw coordinates"
    )
    parser.add_argument(
        "--allow-boundary-estimates",
        action="store_true",
        help="Use explicitly labelled conzone boundary estimates for missing JCs",
    )
    args = parser.parse_args()
    route = GyeongbuRoute.load()
    offices = pd.read_parquet(DATA_PROCESSED_DIR / "tcs_offices_gyeongbu.parquet")
    raw = json.loads(args.ic_file.read_text(encoding="utf-8-sig"))
    if args.jc_coordinate_file:
        supplied = json.loads(args.jc_coordinate_file.read_text(encoding="utf-8"))
        if any(row.get("position_source") != "moct_link_attachment_projection" for row in supplied):
            raise ValueError("Supplemental JC coordinates must retain MOCT source identity")
        existing = {str(row["icCode"]) for row in raw}
        if any(str(row["icCode"]) in existing for row in supplied):
            raise ValueError("Supplemental JC coordinate codes already exist")
        raw.extend(supplied)
    conzones = pd.read_parquet(DATA_PROCESSED_DIR / "conzone_gyeongbu.parquet")

    def normalize(name):
        return str(name).upper().replace("분기점", "").replace("JCT", "JC")

    boundaries = {
        normalize(boundary)
        for name in conzones.loc[conzones["in_corridor"], "name"].dropna()
        for boundary in name.split("-")
        if boundary.upper().endswith(("JC", "JCT"))
    }
    if args.ic_all_file:
        extra = json.loads(args.ic_all_file.read_text(encoding="utf-8-sig"))
        codes = {str(row["icCode"]) for row in raw}
        for row in extra:
            if normalize(row["icName"]) in boundaries and str(row["icCode"]) not in codes:
                raw.append(row)
                codes.add(str(row["icCode"]))
    if args.allow_boundary_estimates:
        raw_names = {normalize(row["icName"]) for row in raw}
        for name in sorted(boundaries - raw_names):
            positions = []
            for row in conzones.loc[conzones["in_corridor"]].to_dict("records"):
                ends = str(row["name"]).split("-")
                if len(ends) != 2:
                    raise ValueError(f"Unexpected conzone name: {row['name']}")
                for boundary, field in zip(ends, ("offset_km_start", "offset_km_end"), strict=True):
                    if normalize(boundary) == name:
                        offset = float(row[field])
                        if pd.isna(offset):
                            raise ValueError(f"Missing boundary position: {name}")
                        positions.append(
                            offset if row["direction"] == "UP" else route.length_km - offset
                        )
            if not positions or max(positions) - min(positions) > 0.002:
                raise ValueError(f"Inconsistent directional/period boundary positions: {name}")
            raw.append(
                {
                    "icCode": f"estimated:{name}",
                    "icName": name,
                    "estimated_milepost_km": sum(positions) / len(positions),
                    "position_source": "conzone_boundary_estimate",
                }
            )
    duplicates = []
    jc_groups = {}
    for row in raw:
        if str(row["icName"]).upper().endswith(("JC", "JCT")):
            jc_groups.setdefault(normalize(row["icName"]), []).append(row)
    removed = set()
    for name, candidates in jc_groups.items():
        if len(candidates) < 2:
            continue
        positions = []
        for row in conzones.loc[conzones["in_corridor"]].to_dict("records"):
            ends = str(row["name"]).split("-")
            if len(ends) != 2:
                continue
            for boundary, field in zip(ends, ("offset_km_start", "offset_km_end"), strict=True):
                if normalize(boundary) == name and pd.notna(row[field]):
                    offset = float(row[field])
                    positions.append(
                        offset if row["direction"] == "UP" else route.length_km - offset
                    )
        if not positions or max(positions) - min(positions) > 0.002:
            raise ValueError(f"Cannot resolve duplicate JC name using VDS boundaries: {name}")
        target = sum(positions) / len(positions)
        ranked = []
        for candidate in candidates:
            milepost, snap = route.project(float(candidate["yValue"]), float(candidate["xValue"]))
            ranked.append((abs(milepost - target), str(candidate["icCode"]), milepost, snap))
        ranked.sort()
        if ranked[0][0] > 0.5 or abs(ranked[0][0] - ranked[1][0]) < 0.01:
            raise ValueError(f"Ambiguous duplicate JC candidates: {name}")
        selected = ranked[0][1]
        for gap, code, milepost, snap in ranked:
            duplicates.append(
                {
                    "name": name,
                    "ic_code": code,
                    "milepost_km": milepost,
                    "boundary_milepost_km": target,
                    "boundary_gap_km": gap,
                    "snap_distance_km": snap,
                    "selected": code == selected,
                    "selection_basis": "existing_vds_boundary_alignment",
                }
            )
            if code != selected:
                removed.add(code)
    raw = [row for row in raw if str(row["icCode"]) not in removed]
    nodes, audit, nearby = build_nodes(offices, raw, route)
    mapped = {normalize(name) for name in nodes.loc[nodes["kind"] == "junction", "name"]}
    unresolved = sorted(boundaries - mapped)
    DATA_PROCESSED_DIR.mkdir(parents=True, exist_ok=True)
    nodes.to_parquet(DATA_PROCESSED_DIR / "od_nodes_gyeongbu.parquet", index=False)
    audit.to_csv(DATA_PROCESSED_DIR / "od_junction_audit.csv", index=False)
    nearby.to_csv(DATA_PROCESSED_DIR / "od_nodes_nearby.csv", index=False)
    pd.DataFrame(duplicates).to_csv(DATA_PROCESSED_DIR / "od_junction_duplicates.csv", index=False)
    print(f"Shared route length: {route.length_km:.3f} km")
    print(nodes.groupby(["direction", "kind"]).size().to_string())
    print("\nJC projection audit:")
    print(audit.to_string(index=False))
    if duplicates:
        print("\nDuplicate JC selection audit:")
        print(pd.DataFrame(duplicates).to_string(index=False))
    print("\nNearby node candidates (not merged):")
    print(nearby.to_string(index=False))
    print("\nConzone JC boundaries without mapped coordinates:", unresolved)
    print("\nNode candidates saved. Direction restrictions and JC movements remain unverified.")


def supplement(nodes, raw, route):
    additions, audit = ([], [])
    for boundary, raw_name in (
        ("대왕판교IC", "대왕판교IC"),
        ("판교IC", "판교IC"),
        ("서영천Hi", "서영천IC"),
    ):
        candidates = [
            row
            for row in raw
            if row["icName"] == raw_name and str(row["routeNo"]) in ("001", "0010")
        ]
        if len(candidates) != 1:
            raise ValueError(f"Expected one Gyeongbu raw IC: {raw_name}, got {len(candidates)}")
        row = candidates[0]
        milepost, snap = route.project(float(row["yValue"]), float(row["xValue"]))
        if snap > 2 or not 0 < milepost < route.length_km:
            raise ValueError(f"Raw IC is not an interior shared-route candidate: {raw_name}")
        node_id = f"ic:{row['icCode']}"
        if nodes["node_id"].eq(node_id).any():
            raise ValueError(f"Candidate already present: {node_id}")
        for direction in ("UP", "DOWN"):
            additions.append(
                {
                    "node_id": node_id,
                    "name": raw_name,
                    "kind": "interchange",
                    "direction": direction,
                    "milepost_km": milepost,
                    "offset_km": route.to_direction(milepost, direction),
                    "office_code": None,
                    "source": "ic_raw_projection_supplement",
                }
            )
        audit.append(
            {
                "boundary_name": boundary,
                "raw_name": raw_name,
                "node_id": node_id,
                "milepost_km": milepost,
                "snap_distance_km": snap,
                "status": "coordinate_candidate_movement_unverified",
            }
        )
    result = pd.concat([nodes, pd.DataFrame(additions)], ignore_index=True)
    if result.duplicated(["direction", "node_id"]).any():
        raise ValueError("Duplicate directional node identity")
    return (result.sort_values(["direction", "offset_km", "node_id"]), pd.DataFrame(audit))


def supplement_main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ic-all-file", type=Path, required=True)
    args = parser.parse_args()
    nodes = pd.read_parquet(DATA_PROCESSED_DIR / "od_nodes_gyeongbu.parquet")
    raw = json.loads(args.ic_all_file.read_text(encoding="utf-8-sig"))
    result, audit = supplement(nodes, raw, GyeongbuRoute.load())
    path = DATA_PROCESSED_DIR / "od_nodes_gyeongbu_supplemented.parquet"
    result.to_parquet(path, index=False)
    audit.to_csv(
        DATA_PROCESSED_DIR / "od_nodes_supplement_audit.csv", index=False, encoding="utf-8-sig"
    )
    print(result.groupby(["direction", "kind"]).size().to_string())
    print(audit.to_string(index=False))
    print("Unresolved: Yeongnak IC coordinates; Tongdosa Hi identity/direction restrictions.")
    print("Other coordinate gaps and Sangseo VDS discrepancy remain under review.")
    print(f"Saved separate candidate table: {path}")


def select_attachments(links, route, transformer):
    nodes = defaultdict(list)
    for row in links:
        for field, xy in [("F_NODE", row["start_xy"]), ("T_NODE", row["end_xy"])]:
            nodes[row[field]].append((row, xy))
    candidates = []
    for node_id, edges in nodes.items():
        names = {row["ROAD_NAME"] for row, xy in edges}
        if "경부고속도로" not in names:
            continue
        for name, other in [("언양JC", "울산고속도로"), ("옥산JC", "옥산오창고속도로")]:
            if other not in names:
                continue
            xy = edges[0][1]
            if any(
                (abs(point[0] - xy[0]) > 1 or abs(point[1] - xy[1]) > 1 for row, point in edges)
            ):
                raise ValueError(f"Inconsistent coordinates for node {node_id}")
            lon, lat = transformer.transform(*xy)
            milepost, distance = route.project(lat, lon)
            if not 0 < milepost < route.length_km or distance > 2:
                continue
            candidates.append(
                dict(
                    name=name,
                    moct_node_id=node_id,
                    raw_x=xy[0],
                    raw_y=xy[1],
                    longitude=lon,
                    latitude=lat,
                    milepost_km=milepost,
                    snap_distance_km=distance,
                    connecting_road=other,
                    link_ids=";".join(sorted({r["LINK_ID"] for r, p in edges})),
                )
            )
    table = pd.DataFrame(candidates)
    if table.empty or set(table.name) != {"언양JC", "옥산JC"}:
        raise ValueError("Both junctions must have actual standard-link connections")
    table["selected"] = False
    for _name, group in table.groupby("name"):
        chosen = group.sort_values(["snap_distance_km", "moct_node_id"]).index[0]
        table.loc[chosen, "selected"] = True
    return table


def replace_coordinates(nodes, evidence, route):
    result = nodes.copy()
    for row in evidence[evidence.selected].itertuples():
        mask = result.node_id.isin([f"jc:estimated:{row.name}", f"jc:moct:{row.moct_node_id}"])
        if set(result.loc[mask, "direction"]) != {"DOWN", "UP"} or mask.sum() != 2:
            raise ValueError(f"Expected exactly two matching directional rows for {row.name}")
        for index in result.index[mask]:
            direction = result.at[index, "direction"]
            result.at[index, "node_id"] = f"jc:moct:{row.moct_node_id}"
            result.at[index, "milepost_km"] = row.milepost_km
            result.at[index, "offset_km"] = route.to_direction(row.milepost_km, direction)
            result.at[index, "source"] = "moct_link_attachment_projection"
    if result.duplicated(["direction", "node_id"]).any():
        raise ValueError("Duplicate replaced node IDs")
    return result.sort_values(["direction", "offset_km", "node_id"])


def file_digest(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def coordinates_main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shp-file", type=Path, required=True)
    parser.add_argument("--nodes-file", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--route-file", type=Path, required=True)
    parser.add_argument("--source-crs", default="EPSG:5186")
    args = parser.parse_args()
    reader = shapefile.Reader(str(args.shp_file), encoding="cp949")
    fields = ["LINK_ID", "F_NODE", "T_NODE", "ROAD_NO", "ROAD_NAME", "ROAD_RANK", "ROAD_TYPE"]
    links = []
    for record in reader.iterRecords(fields=fields):
        row = record.as_dict()
        if row["ROAD_RANK"] == "101" and row["ROAD_NAME"] in {
            "경부고속도로",
            "울산고속도로",
            "옥산오창고속도로",
        }:
            shape = reader.shape(record.oid)
            row["start_xy"], row["end_xy"] = (shape.points[0], shape.points[-1])
            links.append(row)
    route = GyeongbuRoute.load(args.route_file, validate=True)
    transformer = Transformer.from_crs(args.source_crs, "EPSG:4326", always_xy=True)
    evidence = select_attachments(links, route, transformer)
    nodes = pd.read_parquet(args.nodes_file)
    corrected = replace_coordinates(nodes, evidence, route)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    corrected.to_parquet(
        args.output_dir / "od_nodes_gyeongbu_coordinate_completed.parquet", index=False
    )
    evidence.to_csv(args.output_dir / "od_jc_coordinate_evidence.csv", index=False)
    raw = []
    for row in evidence[evidence.selected].itertuples():
        raw.append(
            dict(
                icCode=f"moct:{row.moct_node_id}",
                icName=row.name,
                routeNo="0010",
                xValue=row.longitude,
                yValue=row.latitude,
                position_source="moct_link_attachment_projection",
                source_node_id=row.moct_node_id,
                source_link_ids=row.link_ids,
            )
        )
    (args.output_dir / "ic_junction_coordinates.json").write_text(
        json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    metadata = dict(
        source="ITS standard node/link MOCT_LINK",
        source_crs=args.source_crs,
        source_url="https://www.its.go.kr/nodelink/nodelinkRef",
        selection="actual shared road node with least distance to shared centerline",
        interpretation="representative junction attachment, not geometric JC center or all ramp movements",
        input_sha256={
            str(path.name): file_digest(path)
            for path in [
                args.shp_file,
                args.shp_file.with_suffix(".dbf"),
                args.route_file,
                args.nodes_file,
            ]
        },
    )
    (args.output_dir / "od_jc_coordinate_parameters.json").write_text(
        json.dumps(metadata, indent=2), encoding="utf-8"
    )
    print(evidence.to_string(index=False))


def main():
    actions = {"build": build_main, "supplement": supplement_main, "coordinates": coordinates_main}
    command = sys.argv.pop(1) if len(sys.argv) > 1 and sys.argv[1] in actions else "build"
    actions[command]()


if __name__ == "__main__":
    main()
