from collections import Counter

from . import finding, rule


@rule("intent-mismatch", "warn")
def intent_mismatch(ctx):
    intents = ctx.meta.get("output_intents") or []
    out = []
    for oi in intents:
        ident = oi.get("output_condition_identifier")
        if not ctx.press.matches_condition(ident) and not ctx.press.matches_condition(oi.get("info")):
            out.append(finding(
                "intent-mismatch", [],
                f"File output intent '{ident}' ({oi.get('info')}) differs from press condition "
                f"'{ctx.press.description}'. The Fiery treats these values as {ctx.press.name} without correction.",
                file_intent=ident, press=ctx.press.name))
    return out


@rule("multiple-output-intents", "warn")
def multiple_output_intents(ctx):
    intents = ctx.meta.get("output_intents") or []
    merge = ctx.meta.get("merge")
    out = []
    if len(intents) != 1:
        out.append(finding("multiple-output-intents", [],
                           f"{len(intents)} output intents in file (expected exactly one)",
                           intents=[i.get("output_condition_identifier") for i in intents]))
    if merge:
        carried = {s["source"]: [i.get("output_condition_identifier") for i in s["output_intents"]]
                   for s in merge.get("source_output_intents", [])}
        written = merge.get("output_intent")
        if len({tuple(v) for v in carried.values()}) > 1 or any(v != [written] for v in carried.values()):
            parts = [f"{name}: {', '.join(v) if v else 'none'}" for name, v in carried.items()]
            out.append(finding("multiple-output-intents", [],
                               f"Merged sources carried {'; '.join(parts)}. The engine wrote one intent: {written}.",
                               sources=carried, written=written))
    return out


@rule("mixed-creator", "warn")
def mixed_creator(ctx):
    """Pages whose producer fingerprint (PieceInfo apps + keys) differs from the majority."""
    sources = ctx.meta.get("page_sources") or []
    prints = {s["page"]: tuple(sorted((app, tuple(v["keys"])) for app, v in s["apps"].items())) for s in sources}
    if len(set(prints.values())) <= 1:
        return []
    majority = Counter(prints.values()).most_common(1)[0][0]
    odd = sorted(p for p, fp in prints.items() if fp != majority)
    major_keys = {k for _, keys in majority for k in keys}
    details = []
    for p in odd:
        keys = {k for _, ks in prints[p] for k in ks}
        extra, missing = sorted(keys - major_keys), sorted(major_keys - keys)
        apps = sorted(a for a, _ in prints[p]) or ["none"]
        mod = [v.get("last_modified") for s in sources if s["page"] == p for v in s["apps"].values()]
        details.append(f"p{p} ({'/'.join(apps)}; extra {extra or '-'}; missing {missing or '-'}; "
                       f"exported {', '.join(m for m in mod if m) or '?'})")
    return [finding("mixed-creator", odd,
                    f"{len(odd)} page(s) built differently from the other {len(prints) - len(odd)}: " + "; ".join(details))]


@rule("device-profile-missing", "info")
def device_profile_missing(ctx):
    dev = ctx.device
    if dev and not dev.get("device_profile_file"):
        return [finding("device-profile-missing", [],
                        f"{dev['name']} has no ICC in the device manifest; proof shows {ctx.press.description} "
                        "and the press match depends on house curves.", device=dev["id"])]
    return []


@rule("unapproved-file", "error")
def unapproved_file(ctx):
    approval = ctx.meta.get("approval")
    submitted = ctx.meta.get("submitted_sha256")
    if approval and submitted and submitted != approval.get("sha256"):
        return [finding("unapproved-file", [],
                        f"Submitted file {submitted[:12]}… differs from approved {approval['sha256'][:12]}…",
                        approved=approval["sha256"], submitted=submitted)]
    return []
