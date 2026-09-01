"""Phase 4: p50/p95 latency per pipeline stage, computed from history.jsonl."""

from statistics import quantiles

from dictate import history

STAGES = ("finalize", "transcribe", "cleanup", "paste")


def stats(sample_size: int = 100) -> dict[str, dict[str, float]]:
    """{stage: {"p50": ms, "p95": ms, "n": count}} over recent history."""
    entries = history.last(sample_size)
    out: dict[str, dict[str, float]] = {}
    per_stage: dict[str, list[float]] = {s: [] for s in (*STAGES, "total")}
    for e in entries:
        lat = e.get("latency_ms", {})
        vals = [lat[s] for s in STAGES if s in lat]
        for s in STAGES:
            if s in lat:
                per_stage[s].append(lat[s])
        if vals:
            per_stage["total"].append(sum(vals))
    for stage, vals in per_stage.items():
        if not vals:
            continue
        if len(vals) == 1:
            p50 = p95 = vals[0]
        else:
            cuts = quantiles(vals, n=100, method="inclusive")
            p50, p95 = cuts[49], cuts[94]
        out[stage] = {"p50": round(p50), "p95": round(p95), "n": len(vals)}
    return out


def format_stats(sample_size: int = 100) -> str:
    st = stats(sample_size)
    if not st:
        return "No dictations logged yet."
    n = st["total"]["n"] if "total" in st else 0
    lines = [f"Latency over last {n} dictations (ms):", ""]
    lines.append(f"{'stage':<12}{'p50':>8}{'p95':>8}")
    for stage in (*STAGES, "total"):
        if stage in st:
            lines.append(f"{stage:<12}{st[stage]['p50']:>8}{st[stage]['p95']:>8}")
    return "\n".join(lines)
