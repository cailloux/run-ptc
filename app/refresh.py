"""Nightly city refresh: import a layer only when the city has changed it.

Each layer's signature (feature count, highest OID, latest edit date) costs
one statistics request. When it differs from the signature stored at the last
successful import, the layer is imported, the routing graph rebuilt, and a
change report written: what changed, and what that did to the totals.
"""

from dataclasses import dataclass, field

import httpx2
import psycopg

from app import exclusions as excl
from app.arcgis import LayerSignature, fetch_features, layer_signature
from app.config import Settings
from app.graph import GraphReport, build_graph, graph_report
from app.importer import LAYERS, METERS_PER_MILE, ImportReport, Layer, import_layer
from app.matching import metrics

LAYER_LABEL = {"cartpath": "cart paths", "road": "roads"}
LIST_CAP = 25   # segments listed per added/changed/removed list


def stored_signature(conn: psycopg.Connection, layer: str) -> LayerSignature | None:
    row = conn.execute(
        "SELECT feature_count, max_oid, max_edited_at FROM source_signature WHERE layer = %s",
        (layer,),
    ).fetchone()
    return LayerSignature(*row) if row else None


def store_signature(conn: psycopg.Connection, layer: str, sig: LayerSignature) -> None:
    conn.execute("""
        INSERT INTO source_signature (layer, feature_count, max_oid, max_edited_at, imported_at)
        VALUES (%s, %s, %s, %s, now())
        ON CONFLICT (layer) DO UPDATE SET
            feature_count = EXCLUDED.feature_count, max_oid = EXCLUDED.max_oid,
            max_edited_at = EXCLUDED.max_edited_at, imported_at = EXCLUDED.imported_at
    """, (layer, sig.feature_count, sig.max_oid, sig.max_edited_at))


@dataclass
class Snapshot:
    completion: dict
    counted: dict            # layer -> (segments, miles)
    second_carriageways: int
    components: int
    islands: int


def snapshot(conn: psycopg.Connection) -> Snapshot:
    counted = {layer: (0, 0.0) for layer in LAYERS}
    for layer, n, m in conn.execute("""
        SELECT layer, count(*), coalesce(sum(length_m), 0) / %s FROM segment
        WHERE counted AND NOT excluded GROUP BY layer
    """, (METERS_PER_MILE,)):
        counted[layer] = (n, m)
    second = conn.execute(
        "SELECT count(*) FROM segment WHERE uncounted_reason = 'second carriageway'").fetchone()[0]
    graph = graph_report(conn)
    return Snapshot(metrics(conn).as_dict(), counted, second, graph.components, len(graph.islands))


def _segment_line(seg: tuple) -> str:
    oid, name, seg_type, length_m = seg
    return f"{oid} {name or '(no name)'} ({seg_type or '?'}, {length_m:.0f} m)"


def _list_lines(title: str, segments: list[tuple]) -> list[str]:
    if not segments:
        return []
    lines = [f"  {title}:"] + [f"    {_segment_line(s)}" for s in segments[:LIST_CAP]]
    if len(segments) > LIST_CAP:
        lines.append(f"    … and {len(segments) - LIST_CAP} more")
    return lines


@dataclass
class RefreshReport:
    signatures: dict[str, LayerSignature] = field(default_factory=dict)
    imports: dict[str, ImportReport] = field(default_factory=dict)
    before: Snapshot | None = None
    after: Snapshot | None = None
    graph: GraphReport | None = None

    @property
    def changed(self) -> bool:
        return bool(self.imports)

    def headline(self) -> str:
        if not self.changed:
            edits = ", ".join(
                f"{LAYER_LABEL[layer]} last edited "
                f"{sig.max_edited_at.date().isoformat() if sig.max_edited_at else 'never'}"
                for layer, sig in self.signatures.items())
            return f"city data unchanged ({edits})"
        parts = []
        for layer in LAYERS:
            r = self.imports.get(layer)
            parts.append(f"{LAYER_LABEL[layer]}: unchanged" if r is None else
                         f"{LAYER_LABEL[layer]}: {r.added} added, {r.changed} changed, {r.removed} removed")
        return "; ".join(parts)

    def lines(self) -> list[str]:
        lines = [self.headline()]
        if not self.changed:
            return lines
        for layer, r in self.imports.items():
            lines.append(f"{LAYER_LABEL[layer]}:")
            lines += _list_lines("added", r.added_segments)
            lines += _list_lines("changed", r.changed_segments)
            lines += _list_lines("removed", r.removed_segments)
            lines += [f"  {line}" for line in r.lines()]
            lines += [f"  warning: {w}" for w in r.exclusion_warnings]
        b, a = self.before, self.after
        bc, ac = b.completion, a.completion
        lines += [
            "impact (before -> after):",
            f"  cart path miles complete: {bc['cartpath_complete_mi']} -> {ac['cartpath_complete_mi']} "
            f"of {bc['cartpath_total_mi']} -> {ac['cartpath_total_mi']} mi "
            f"({bc['cartpath_pct']}% -> {ac['cartpath_pct']}%)",
            f"  segments complete: {bc['segments_complete']} of {bc['segments_total']} -> "
            f"{ac['segments_complete']} of {ac['segments_total']}",
            f"  road miles covered: {bc['road_covered_mi']} of {bc['road_total_mi']} -> "
            f"{ac['road_covered_mi']} of {ac['road_total_mi']} mi",
        ]
        for layer in LAYERS:
            (bn, bm), (an, am) = b.counted[layer], a.counted[layer]
            lines.append(f"  counted {LAYER_LABEL[layer]}: {bn} ({bm:.2f} mi) -> {an} ({am:.2f} mi)")
        lines += [
            f"  second carriageways: {b.second_carriageways} -> {a.second_carriageways}",
            f"  graph: {b.components} -> {a.components} components, {b.islands} -> {a.islands} islands",
        ]
        return lines


def download(client: httpx2.Client, layer: Layer, sig: LayerSignature, fetch=fetch_features) -> list[dict]:
    """Fetch a layer, refusing a download that doesn't match the signature's
    count. A page lost to a server glitch would otherwise delete real segments
    (and their hits). A city edit between the two requests also fails it; the
    next run retries."""
    features = fetch(client, layer.url, layer.oid_field)
    if len(features) != sig.feature_count:
        raise RuntimeError(f"{layer.name}: downloaded {len(features)} features but the city reports "
                           f"{sig.feature_count}; refusing a partial import")
    return features


def refresh(conn: psycopg.Connection, client: httpx2.Client, settings: Settings,
            exclusions: excl.Exclusions, *, force: bool = False,
            signature=layer_signature, fetch=fetch_features) -> RefreshReport:
    """Import each layer whose signature changed (or every layer if forced)."""
    report = RefreshReport()
    for name, layer in LAYERS.items():
        report.signatures[name] = signature(client, layer.url, layer.oid_field)
    changed = [name for name in LAYERS
               if force or stored_signature(conn, name) != report.signatures[name]]
    if not changed:
        return report

    report.before = snapshot(conn)
    for name in changed:
        layer = LAYERS[name]
        features = download(client, layer, report.signatures[name], fetch)
        report.imports[name] = import_layer(conn, layer, features, settings, exclusions)
    report.graph = build_graph(conn, settings)
    report.after = snapshot(conn)
    # Only now, with everything done, record what was imported; a failure
    # anywhere above leaves the old signatures, so the next run retries.
    for name in changed:
        store_signature(conn, name, report.signatures[name])
    return report
